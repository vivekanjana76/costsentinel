"""The Agent Card, its JWS, the task lifecycle, and the claim that the surface cannot write.

Exercised entirely in-process. The FastAPI test client speaks to the app directly and
nothing opens a socket, which is how CI runs it. `FakeA2AClient` below is the
"in-process fake client": it consumes the surface the way an external agent would. It
fetches the card, verifies the signature against the published key, submits, polls,
validates the typed artefact and cancels.

Three tests carry the weight:

- `test_no_string_on_the_served_card_offers_a_write`. The twenty-two-verb guard,
  re-asserted against everything the fuller card serves (#111).
- `test_a_signed_card_verifies_under_an_independent_jose_implementation`. PyJWT, which
  we did not write, accepts the JWS; a tamper makes it refuse (#109).
- `test_cancelling_changes_no_sweep_checkpoint_or_queue`. The one new POST changes a
  task and nothing else (#110, #111).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from costsentinel.a2a.card import (
    OFFICIAL_BINDINGS,
    PROTOCOL_VERSION,
    READ_ONLY_EXTENSION_URI,
    READ_ONLY_SKILLS,
    AgentCard,
    Json,
    SkillId,
    canonical_payload,
)
from costsentinel.a2a.server import (
    CARD_PATH,
    TASKS_PATH,
    build_router,
    card_for,
    mount,
    signing_key,
)
from costsentinel.a2a.signing import (
    HMAC_HEADER,
    JWKS_PATH,
    SYNTHETIC_KEY_SEED,
    CardSigningError,
    b64url,
    b64url_decode,
    public_jwk,
    sign_card,
    synthetic_key,
    verify_card,
    verify_card_hmac,
)
from costsentinel.a2a.tasks import (
    ARTIFACT_ADAPTER,
    FindingsArtifact,
    QueueArtifact,
    ScanArtifact,
    Task,
    TaskError,
    TaskNotCancelableError,
    TaskState,
    TaskStore,
    UnknownSkillError,
    UnknownTaskError,
)
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.domain.state import ScanState
from costsentinel.graph.approvals import approvals_graph, paused_sweeps, thread_ids
from costsentinel.graph.checkpointer import checkpointer_for
from costsentinel.providers.mock import CLIENT_MINISTRY

#: Verbs that would mean the surface can change something. Matched case-insensitively
#: against every string a consumer reads off the card, and against every route path.
MUTATING_VOCABULARY: tuple[str, ...] = (
    "write",
    "mutate",
    "modify",
    "execute",
    "apply",
    "remediate",
    "delete",
    "destroy",
    "resize",
    "deallocate",
    "stop",
    "start",
    "restart",
    "provision",
    "deprovision",
    "update",
    "patch",
    "create",
    "approve",
    "decide",
    "authorise",
    "authorize",
)

#: Words that make a sentence a denial. A description may name a mutation only to deny it.
DENIALS: tuple[str, ...] = ("no path", "cannot", "never", "nothing", "only reads", "read-only")


def _settings(scratch: Path, *, a2a_enabled: bool = False, **extra: Any) -> Settings:
    """Offline settings, with the A2A surface off unless a test turns it on."""
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=scratch,
        a2a_enabled=a2a_enabled,
        **extra,
    )


class FakeA2AClient:
    """An external agent, in-process: what it fetches, verifies, submits and reads."""

    def __init__(self, settings: Settings, store: TaskStore | None = None) -> None:
        app = FastAPI()
        assert mount(app, settings, store)
        self.http = TestClient(app)

    def card(self) -> tuple[dict[str, Json], dict[str, str]]:
        response = self.http.get(CARD_PATH)
        assert response.status_code == 200
        return response.json(), dict(response.headers)

    def public_key(self) -> ec.EllipticCurvePublicKey:
        """The key from the published JWKS, rebuilt from its coordinates alone."""
        (jwk,) = self.http.get(JWKS_PATH).json()["keys"]
        x = int.from_bytes(b64url_decode(jwk["x"]), "big")
        y = int.from_bytes(b64url_decode(jwk["y"]), "big")
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()

    def submit(self, skill: str, client: str | None = None) -> Task:
        response = self.http.post(TASKS_PATH, json={"skill": skill, "client": client})
        assert response.status_code == 201, response.text
        return Task.model_validate(response.json())

    def status(self, task_id: str) -> Task:
        return Task.model_validate(self.http.get(f"{TASKS_PATH}/{task_id}").json())

    def artifact(self, task_id: str) -> ScanArtifact | FindingsArtifact | QueueArtifact:
        response = self.http.get(f"{TASKS_PATH}/{task_id}/artifact")
        assert response.status_code == 200, response.text
        return ARTIFACT_ADAPTER.validate_python(response.json())

    def cancel(self, task_id: str) -> Any:
        return self.http.post(f"{TASKS_PATH}/{task_id}:cancel")


def _strings(value: Json, key: str = "") -> Iterator[tuple[str, str]]:
    """Every string in a JSON value, with the key it sits under."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, key)
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from _strings(item, name)


def _keys(value: Json) -> Iterator[str]:
    if isinstance(value, dict):
        for name, item in value.items():
            yield name
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _compact(served: dict[str, Json], index: int = 0) -> str:
    """The detached JWS re-attached, per RFC 7515 Appendix F, for a third-party verifier."""
    signatures = served["signatures"]
    assert isinstance(signatures, list)
    entry = signatures[index]
    assert isinstance(entry, dict)
    return f"{entry['protected']}.{b64url(canonical_payload(served))}.{entry['signature']}"


# ---------------------------------------------------------------------------
# The mutation guard, re-asserted against the fuller surface (#111)
# ---------------------------------------------------------------------------


def test_the_vocabulary_is_the_twenty_two_verbs() -> None:
    """Pinned by count, so a verb quietly dropped to make a new skill pass shows up here."""
    assert len(MUTATING_VOCABULARY) == 22
    assert len(set(MUTATING_VOCABULARY)) == 22


def test_the_advertised_skills_contain_no_write_capability() -> None:
    """The load-bearing claim of the A2A surface, on the skills themselves.

    Matched against a vocabulary, not an allowed list, so a skill added later has to
    pass this. Being absent from a list somebody forgot to update would not be enough.
    """
    for skill in READ_ONLY_SKILLS:
        haystacks = {
            "id": skill.id.value,
            "name": skill.name,
            "tags": " ".join(skill.tags),
            "examples": " ".join(skill.examples),
        }
        for where, text in haystacks.items():
            offending = [verb for verb in MUTATING_VOCABULARY if verb in text.lower()]
            assert not offending, f"{skill.id.value} {where}: {offending}"


def test_no_string_on_the_served_card_offers_a_write(tmp_path: Path) -> None:
    """Every string the fuller card serves: skills, the extension, the interface, the JWS.

    Descriptions are prose. They may use a mutating word only in a sentence that
    denies the capability, so for them the denial is what is asserted. Everything else
    -- ids, names, tags, URIs, the binding, the URL -- may not contain one at all.
    """
    served, _ = FakeA2AClient(_settings(tmp_path, a2a_enabled=True)).card()
    checked = 0
    for key, text in _strings(served):
        if key in {"protected", "signature"}:
            continue  # base64url: letters by chance, not words a consumer reads
        lowered = text.lower()
        offending = [verb for verb in MUTATING_VOCABULARY if verb in lowered]
        if key == "description":
            assert not offending or any(word in lowered for word in DENIALS), (key, text)
        else:
            assert not offending, (key, text, offending)
        checked += 1
    assert checked > 20, "the walk found almost nothing, so it is not walking the card"


def test_every_description_says_it_reads_and_the_queue_says_it_cannot_decide() -> None:
    """The denial is not optional: every skill states what it is."""
    for skill in READ_ONLY_SKILLS:
        assert "read" in skill.description.lower(), f"{skill.id.value} does not say it reads"
    queue = next(one for one in READ_ONLY_SKILLS if one.id is SkillId.APPROVAL_QUEUE)
    assert "cannot decide it" in queue.description
    assert "no execution path" in queue.description


def test_the_skill_vocabulary_is_closed_and_entirely_read_only() -> None:
    """Three skills and no fourth. A new member has to be added deliberately."""
    assert {member.value for member in SkillId} == {
        "report.scan",
        "report.findings",
        "approvals.queue",
    }
    assert {skill.id for skill in READ_ONLY_SKILLS} == set(SkillId)


def test_every_route_path_is_free_of_mutating_verbs(tmp_path: Path) -> None:
    """The routes are a surface too. A `/a2a/tasks/{id}:apply` would show here."""
    router = build_router(_settings(tmp_path))
    paths = [getattr(route, "path", "") for route in router.routes]
    assert paths
    for path in paths:
        offending = [verb for verb in MUTATING_VOCABULARY if verb in path.lower()]
        assert not offending, (path, offending)


def test_the_only_posts_create_or_cancel_a_read_task(tmp_path: Path) -> None:
    """Asserted against the routes, not the docstrings.

    Two POSTs. One creates a task, and every task it can create is a read. One cancels
    a task before its read starts. No PUT, no PATCH, no DELETE.
    """
    router = build_router(_settings(tmp_path))
    methods: dict[str, set[str]] = {}
    for route in router.routes:
        path = getattr(route, "path", "")
        methods.setdefault(path, set()).update(getattr(route, "methods", set()) or set())

    writing = {path: verbs & {"PUT", "PATCH", "DELETE"} for path, verbs in methods.items()}
    assert not any(writing.values()), writing

    posts = {path for path, verbs in methods.items() if "POST" in verbs}
    assert posts == {TASKS_PATH, f"{TASKS_PATH}/{{task_id}}:cancel"}, posts


def test_an_external_caller_cannot_reach_a_decision_or_an_execution(tmp_path: Path) -> None:
    """The A2A equivalent of the four no-mutation proofs.

    No advertised skill decides, and the store has no branch that could run one.
    Asking for anything unadvertised fails with the list of what is, and no task exists.
    """
    store = TaskStore(_settings(tmp_path))
    for attempt in ("approvals.decide", "approvals.approve", "remediation.apply", "execute"):
        with pytest.raises(UnknownSkillError, match="not an advertised skill"):
            store.submit(attempt)
    assert store.count == 0, "a refused skill must not create a task"


def test_the_a2a_package_reaches_no_decision_path_and_no_mutating_client() -> None:
    """The structural check, extended to the new modules.

    Nothing here imports a cloud client, and nothing names the functions that record a
    decision or resume a sweep. Reading the queue goes through `paused_sweeps`;
    deciding it would need `resume`, and that word is not in this package.
    """
    imported: set[str] = set()
    names: set[str] = set()
    for path in sorted(Path("src/costsentinel/a2a").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

    assert not any(name.startswith("azure") for name in imported), imported
    assert not any("mgmt" in name for name in imported), imported
    for forbidden in ("resume", "find_pending", "DecisionRequest", "record_decision"):
        assert forbidden not in names, forbidden


def test_cancelling_changes_no_sweep_checkpoint_or_queue(tmp_path: Path) -> None:
    """The new POST changes a *task*, an in-memory record this surface owns.

    Not a paused sweep, not its checkpoint and not the approval queue. Everything the
    checkpoint holds is snapshotted before and compared after: a queue item cannot
    disappear because a task asking about it was cancelled.
    """
    settings = _settings(tmp_path)
    store = TaskStore(settings)
    assert store.execute(SkillId.SCAN, {"client": CLIENT_MINISTRY}).state is TaskState.COMPLETED

    def _snapshot() -> dict[str, Any]:
        with checkpointer_for(settings) as saver:
            graph = approvals_graph(settings, saver)
            states = {
                run_id: ScanState.model_validate(
                    graph.get_state({"configurable": {"thread_id": run_id}}).values
                ).model_dump(mode="json")
                for run_id in thread_ids(saver)
            }
            queue = [
                item.recommendation_id for s in paused_sweeps(graph, saver) for item in s.pending
            ]
        return {"states": states, "queue": queue}

    before = _snapshot()
    assert before["queue"], "the scan should have left something awaiting a decision"

    for skill in SkillId:
        task = store.submit(skill, {"client": CLIENT_MINISTRY})
        assert store.cancel(task.task_id).state is TaskState.CANCELED
        assert store.run(task.task_id).state is TaskState.CANCELED

    assert _snapshot() == before


# ---------------------------------------------------------------------------
# The card in the A2A v1 wire shape (#108)
# ---------------------------------------------------------------------------


def test_the_card_is_in_the_a2a_v1_wire_shape(tmp_path: Path) -> None:
    """What a verifier written against the spec expects, or the signature cannot verify.

    The REQUIRED fields of `a2a.proto`'s AgentCard, camelCase throughout, and no
    top-level `url` or `protocolVersion`: v1 moved both into `supportedInterfaces`.
    """
    served, _ = FakeA2AClient(_settings(tmp_path, a2a_enabled=True)).card()
    for required in (
        "name",
        "description",
        "supportedInterfaces",
        "version",
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    ):
        assert served.get(required), required
    assert "url" not in served
    assert "protocolVersion" not in served
    assert not [key for key in _keys(served) if "_" in key], "a snake_case key reached the wire"

    interfaces = served["supportedInterfaces"]
    assert isinstance(interfaces, list)
    (interface,) = interfaces
    assert isinstance(interface, dict)
    assert interface["protocolVersion"] == PROTOCOL_VERSION == "1.0"
    assert interface["url"] == "http://localhost:8000"


def test_absent_fields_are_omitted_and_explicit_optionals_kept(tmp_path: Path) -> None:
    """Spec 8.4.1's presence rule: empties go, an explicit optional `false` stays."""
    served = card_for(_settings(tmp_path)).wire()

    def _no_empties(value: Json) -> None:
        assert value is not None
        if isinstance(value, list | dict | str):
            assert len(value) > 0
        if isinstance(value, list):
            for item in value:
                _no_empties(item)
        if isinstance(value, dict):
            for item in value.values():
                _no_empties(item)

    _no_empties(served)
    capabilities = served["capabilities"]
    assert isinstance(capabilities, dict)
    assert capabilities["streaming"] is False
    assert capabilities["pushNotifications"] is False


def test_the_card_declares_itself_read_only_as_an_extension(tmp_path: Path) -> None:
    """Declared where the spec puts claims it has no word for, not as an unknown field."""
    card = card_for(_settings(tmp_path))
    assert card.read_only
    assert READ_ONLY_EXTENSION_URI in {ext.uri for ext in card.capabilities.extensions}
    assert "no path to change a resource" in card.description
    assert "readOnly" not in card.wire()


def test_the_served_card_round_trips_through_the_model(tmp_path: Path) -> None:
    """A consumer parsing the JSON gets back exactly the card that was signed."""
    card = card_for(_settings(tmp_path))
    assert AgentCard.model_validate(card.wire()) == card


@pytest.mark.xfail(
    reason="routes are an A2A-shaped task API, not the HTTP+JSON binding; closes #113",
    strict=True,
)
def test_the_interface_speaks_an_official_a2a_binding(tmp_path: Path) -> None:
    """The target: a conformant A2A client can talk to this agent without special-casing.

    Today the card declares `COSTSENTINEL-TASKS`, because claiming `HTTP+JSON` would
    invite a client to send `SendMessage` requests nothing here understands.
    """
    (interface,) = card_for(_settings(tmp_path)).supported_interfaces
    assert interface.protocol_binding in OFFICIAL_BINDINGS


# ---------------------------------------------------------------------------
# JWS signing per A2A v1 section 8.4 (#109)
# ---------------------------------------------------------------------------

#: Spec section 8.4.1's own example, before and after canonicalisation. Verbatim.
SPEC_EXAMPLE: dict[str, Json] = {
    "name": "Example Agent",
    "description": "An example agent that echoes messages.",
    "supportedInterfaces": [
        {
            "url": "https://example.com/a2a/v1",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ],
    "version": "1.0.0",
    "capabilities": {"streaming": False, "pushNotifications": False, "extensions": []},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {"id": "echo", "name": "Echo", "description": "Echoes the input.", "tags": ["echo"]}
    ],
}
SPEC_CANONICAL = (
    '{"capabilities":{"pushNotifications":false,"streaming":false},'
    '"defaultInputModes":["text/plain"],"defaultOutputModes":["text/plain"],'
    '"description":"An example agent that echoes messages.","name":"Example Agent",'
    '"skills":[{"description":"Echoes the input.","id":"echo","name":"Echo","tags":["echo"]}],'
    '"supportedInterfaces":[{"protocolBinding":"JSONRPC","protocolVersion":"1.0",'
    '"url":"https://example.com/a2a/v1"}],"version":"1.0.0"}'
)


def test_canonicalisation_matches_the_specs_own_example() -> None:
    """Byte for byte, on the spec's vector -- presence rule and RFC 8785 together.

    The empty `extensions` is dropped and the explicit `false`s are kept, exactly as
    8.4.1 walks through. Canonicalisation is where a signature that "works" against
    our own verifier and fails against everybody else's would come from.
    """
    assert canonical_payload(SPEC_EXAMPLE).decode() == SPEC_CANONICAL
    assert (
        canonical_payload({**SPEC_EXAMPLE, "signatures": [{"x": "y"}]}).decode() == SPEC_CANONICAL
    )


def test_a_signed_card_verifies_under_an_independent_jose_implementation(tmp_path: Path) -> None:
    """PyJWT, which we did not write, accepts the JWS over the card it was served.

    The served JSON is what is verified, through the public key fetched from the
    published JWKS: the same steps spec 8.4.3 gives a client. Then one character of
    the card is changed and PyJWT refuses it, so the acceptance is not vacuous.
    """
    client = FakeA2AClient(_settings(tmp_path, a2a_enabled=True))
    served, _ = client.card()
    key = client.public_key()

    decoded = jwt.api_jws.decode_complete(_compact(served), key=key, algorithms=["ES256"])
    assert decoded["payload"] == canonical_payload(served)
    assert verify_card(served, key)

    tampered = {**served, "description": f"{served['description']}!"}
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.api_jws.decode_complete(_compact(tampered), key=key, algorithms=["ES256"])
    assert not verify_card(tampered, key)


def test_the_protected_header_is_what_the_spec_requires(tmp_path: Path) -> None:
    """alg, typ and kid (MUST), jku (MAY). kid is the key's thumbprint, so it is derived."""
    settings = _settings(tmp_path)
    (signature,) = card_for(settings).signatures
    header = json.loads(b64url_decode(signature.protected))
    assert header == {
        "alg": "ES256",
        "typ": "JOSE",
        "kid": public_jwk(synthetic_key().public_key())["kid"],
        "jku": f"http://localhost:8000{JWKS_PATH}",
    }
    # Raw r || s, 64 bytes, per RFC 7518 -- not the DER the crypto library emits.
    assert len(b64url_decode(signature.signature)) == 64


@pytest.mark.parametrize(
    "tamper",
    [
        lambda c: {**c, "description": "now with execution"},
        lambda c: {**c, "skills": [*c["skills"], {**c["skills"][0], "id": "remediation.apply"}]},
        lambda c: {
            **c,
            "supportedInterfaces": [{**c["supportedInterfaces"][0], "url": "https://evil"}],
        },
        lambda c: {**c, "capabilities": {**c["capabilities"], "extensions": []}},
        lambda c: {**c, "version": "9.9.9"},
    ],
    ids=["description", "added-skill", "redirected-url", "dropped-read-only", "version"],
)
def test_any_tamper_fails_verification(tmp_path: Path, tamper: Any) -> None:
    """Including the two that matter most: an added write skill, and a dropped read-only.

    Somebody adding a capability in transit, or stripping the declaration that the
    agent only reads, must not be able to leave the signature intact.
    """
    served = card_for(_settings(tmp_path)).wire()
    assert verify_card(served, synthetic_key().public_key())
    assert not verify_card(tamper(served), synthetic_key().public_key())


def test_a_signature_under_another_key_does_not_verify(tmp_path: Path) -> None:
    """The kid check and the signature check both refuse a stranger's key."""
    served = card_for(_settings(tmp_path)).wire()
    stranger = ec.generate_private_key(ec.SECP256R1())
    assert not verify_card(served, stranger.public_key())

    resigned = sign_card(card_for(_settings(tmp_path)), stranger, jku="https://x").wire()
    assert verify_card(resigned, stranger.public_key())
    assert not verify_card(resigned, synthetic_key().public_key())


@pytest.mark.parametrize("alg", ["none", "HS256", "ES384", "RS256"])
def test_algorithm_confusion_is_refused(tmp_path: Path, alg: str) -> None:
    """Only ES256 is accepted. `none` and HMAC are the textbook downgrade attacks."""
    served = card_for(_settings(tmp_path)).wire()
    signatures = served["signatures"]
    assert isinstance(signatures, list)
    entry = signatures[0]
    assert isinstance(entry, dict)
    protected = entry["protected"]
    assert isinstance(protected, str)
    header = json.loads(b64url_decode(protected))
    forged = {**entry, "protected": b64url(json.dumps({**header, "alg": alg}).encode())}
    assert not verify_card({**served, "signatures": [forged]}, synthetic_key().public_key())


def test_malformed_signatures_do_not_verify_and_do_not_raise(tmp_path: Path) -> None:
    """A received card is untrusted input. Garbage in it is a False, not a crash."""
    card = card_for(_settings(tmp_path))
    served = card.wire()
    key = synthetic_key().public_key()
    genuine = card.signatures[0].protected
    junks: tuple[Json, ...] = (
        "not a list",
        [{"protected": 1, "signature": 2}],
        [{"protected": "!!!", "signature": "@@@"}],
        [{"protected": b64url(b"[]"), "signature": b64url(b"x" * 64)}],
        [{"protected": genuine, "signature": b64url(b"short")}],
    )
    for junk in junks:
        assert not verify_card({**served, "signatures": junk}, key)


def test_real_mode_without_a_key_serves_an_unsigned_card(tmp_path: Path) -> None:
    """A signature anybody can forge is worse than none: it invites trust.

    And an unsigned card does not verify by vacuity, and publishes no key.
    """
    real = _settings(tmp_path, a2a_enabled=True).model_copy(update={"mode": RunMode.REAL})
    assert signing_key(real) is None
    served = card_for(real).wire()
    assert "signatures" not in served
    assert not verify_card(served, synthetic_key().public_key())
    assert FakeA2AClient(real).http.get(JWKS_PATH).json() == {"keys": []}


def test_mock_mode_signs_with_a_derived_key_and_no_private_key_is_committed() -> None:
    """CI signs and verifies on every run, and the repository holds no PEM.

    The seed says it is worthless, and the key it yields is deterministic, so the kid
    is stable across runs.
    """
    assert "not-a-secret" in SYNTHETIC_KEY_SEED
    assert "synthetic" in SYNTHETIC_KEY_SEED
    assert public_jwk(synthetic_key().public_key()) == public_jwk(synthetic_key().public_key())
    marker = "-----BEGIN " + "PRIVATE KEY-----"  # split so this file is not itself a match
    for path in Path("src").rglob("*.py"):
        assert marker not in path.read_text(encoding="utf-8"), path


def test_a_configured_key_file_signs_in_any_mode(tmp_path: Path) -> None:
    """An operator's own P-256 key signs, and the published JWKS is its public half."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = tmp_path / "card-key.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    settings = _settings(tmp_path, a2a_enabled=True, a2a_jws_key_file=pem).model_copy(
        update={"mode": RunMode.REAL}
    )
    client = FakeA2AClient(settings)
    served, _ = client.card()
    assert verify_card(served, key.public_key())
    assert public_jwk(client.public_key()) == public_jwk(key.public_key())
    (published,) = client.http.get(JWKS_PATH).json()["keys"]
    assert "d" not in published, "the JWKS must never carry the private scalar"


def test_a_wrong_or_unreadable_key_file_fails_loudly(tmp_path: Path) -> None:
    """Fails while the app is built, rather than serving an unsigned card nobody notices."""
    p384 = tmp_path / "p384.pem"
    p384.write_bytes(
        ec.generate_private_key(ec.SECP384R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    for path, expected in ((p384, "P-256"), (tmp_path / "missing.pem", "could not be loaded")):
        with pytest.raises(CardSigningError, match=expected):
            build_router(_settings(tmp_path, a2a_jws_key_file=path))


def test_the_hmac_fallback_is_out_of_band_and_never_poses_as_a_jws(tmp_path: Path) -> None:
    """Kept for consumers that already check it, and labelled as what it is.

    It travels in a CostSentinel header, never in `signatures`, so an A2A client cannot
    mistake a symmetric MAC for the card's signature. It covers the same payload, and
    a tamper breaks it.
    """
    secret = "a-test-secret"  # noqa: S105 -- a test value, not a credential
    served, headers = FakeA2AClient(
        _settings(tmp_path, a2a_enabled=True, a2a_signing_key=secret)
    ).card()
    header = headers[HMAC_HEADER.lower()]
    assert header.startswith("sha256=")
    assert verify_card_hmac(served, secret, header)
    assert not verify_card_hmac({**served, "version": "0"}, secret, header)
    assert not verify_card_hmac(served, "another-secret", header)
    assert not verify_card_hmac(served, secret, "")

    signatures = served["signatures"]
    assert isinstance(signatures, list)
    algorithms = {
        json.loads(b64url_decode(str(entry["protected"])))["alg"]
        for entry in signatures
        if isinstance(entry, dict)
    }
    assert algorithms == {"ES256"}

    _, without = FakeA2AClient(_settings(tmp_path / "b", a2a_enabled=True)).card()
    assert HMAC_HEADER.lower() not in without


def test_the_committed_card_is_the_served_card_and_still_verifies(tmp_path: Path) -> None:
    """The artefact a reviewer checks: `docs/a2a/agent-card.json` must be the real thing.

    It must verify under the committed JWKS, both with our code and with PyJWT, and its
    signed body must equal what the server serves today. A stale file would teach a
    reviewer that something verifies which the server no longer produces. ECDSA
    signatures are randomised, so the comparison is of the canonical payload, not the
    signature bytes. To regenerate after a deliberate change, write
    `card_document(settings)` and `jwks(synthetic_key())` in mock mode with the default
    `A2A_URL` to the two files.
    """
    committed = json.loads(Path("docs/a2a/agent-card.json").read_text(encoding="utf-8"))
    (jwk,) = json.loads(Path("docs/a2a/agent-card-jwks.json").read_text(encoding="utf-8"))["keys"]
    assert jwk == public_jwk(synthetic_key().public_key())
    assert "d" not in jwk

    assert verify_card(committed, synthetic_key().public_key())
    jwt.api_jws.decode_complete(_compact(committed), key=jwt.PyJWK(jwk), algorithms=["ES256"])

    served = card_for(_settings(tmp_path)).wire()
    assert canonical_payload(committed) == canonical_payload(served), (
        "docs/a2a/agent-card.json no longer matches the served card; regenerate it"
    )


# ---------------------------------------------------------------------------
# The task lifecycle (#110)
# ---------------------------------------------------------------------------


def test_submit_returns_submitted_and_the_work_runs_afterwards(tmp_path: Path) -> None:
    """The lifecycle is real now: an id comes back before the read has happened."""
    store = TaskStore(_settings(tmp_path))
    task = store.submit(SkillId.SCAN, {"client": CLIENT_MINISTRY})
    assert task.state is TaskState.SUBMITTED
    assert not task.is_finished
    with pytest.raises(TaskError, match="submitted and has produced no artefact"):
        store.artifact(task.task_id)

    done = store.run(task.task_id)
    assert done.state is TaskState.COMPLETED
    assert done.is_finished
    assert done.updated_at >= task.updated_at
    assert store.status(task.task_id) == done
    assert store.run(task.task_id) == done, "a second run must not run it again"


def test_each_skill_returns_its_typed_artifact_over_http(tmp_path: Path) -> None:
    """Submit, poll, collect -- and validate, the way an external agent would.

    The artefact parses as its skill's own type through `ARTIFACT_ADAPTER`, so a
    consumer is not left reading keys out of an untyped mapping.
    """
    client = FakeA2AClient(_settings(tmp_path, a2a_enabled=True))

    scan = client.submit(SkillId.SCAN.value, CLIENT_MINISTRY)
    assert scan.state is TaskState.SUBMITTED  # as returned; the work runs after
    assert client.status(scan.task_id).state is TaskState.COMPLETED
    review = client.artifact(scan.task_id)
    assert isinstance(review, ScanArtifact)
    assert [report.client for report in review.reports] == [CLIENT_MINISTRY]
    assert review.reports[0].findings
    assert review.awaiting_approval == 2
    assert "no execution path" in review.note

    findings = client.artifact(client.submit(SkillId.FINDINGS.value, CLIENT_MINISTRY).task_id)
    assert isinstance(findings, FindingsArtifact)
    assert findings.sweeps == 1
    assert "No cloud call and no model call" in findings.note

    queue = client.artifact(client.submit(SkillId.APPROVAL_QUEUE.value, CLIENT_MINISTRY).task_id)
    assert isinstance(queue, QueueArtifact)
    assert queue.awaiting == 2
    assert queue.sweeps[0].client == CLIENT_MINISTRY
    assert len(queue.sweeps[0].pending) == 2
    assert "approvals decide" in queue.note
    assert "no path to decide it" in queue.note


def test_the_findings_skill_reads_stored_results_rather_than_rescanning(tmp_path: Path) -> None:
    """The current findings are what was last reported, not what a rescan would say."""
    store = TaskStore(_settings(tmp_path))
    empty = store.artifact(store.execute(SkillId.FINDINGS).task_id)
    assert isinstance(empty, FindingsArtifact)
    assert empty.sweeps == 0

    store.execute(SkillId.SCAN, {"client": CLIENT_MINISTRY})
    after = store.artifact(store.execute(SkillId.FINDINGS).task_id)
    assert isinstance(after, FindingsArtifact)
    assert after.sweeps == 1
    assert after.reports[0].client == CLIENT_MINISTRY


def test_a_cancelled_task_never_runs_its_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel means the read never happened, not that its result was thrown away."""
    store = TaskStore(_settings(tmp_path))

    def _must_not_run(*_: object) -> None:
        pytest.fail("a cancelled task reached its read")

    monkeypatch.setattr(store, "_dispatch", _must_not_run)
    task = store.submit(SkillId.SCAN)
    cancelled = store.cancel(task.task_id)
    assert cancelled.state is TaskState.CANCELED
    assert cancelled.is_finished
    assert store.run(task.task_id).state is TaskState.CANCELED
    with pytest.raises(TaskError, match="canceled and has produced no artefact"):
        store.artifact(task.task_id)


def test_cancel_is_idempotent(tmp_path: Path) -> None:
    """Spec 3.3.1: a repeated cancel has the same effect as the first."""
    store = TaskStore(_settings(tmp_path))
    task = store.submit(SkillId.FINDINGS)
    first = store.cancel(task.task_id)
    assert store.cancel(task.task_id) == first


def test_a_finished_task_is_not_cancelable(tmp_path: Path) -> None:
    """Completed and failed tasks stay what they were; cancel says why it cannot act."""
    store = TaskStore(_settings(tmp_path))
    done = store.execute(SkillId.FINDINGS)
    with pytest.raises(TaskNotCancelableError, match="completed and cannot be cancelled"):
        store.cancel(done.task_id)
    assert store.status(done.task_id) == done

    failing = TaskStore(_settings(tmp_path).model_copy(update={"mode": RunMode.REAL}))
    failed = failing.execute(SkillId.SCAN, {"client": CLIENT_MINISTRY})
    assert failed.state is TaskState.FAILED
    with pytest.raises(TaskNotCancelableError, match="failed"):
        failing.cancel(failed.task_id)


def test_a_working_task_is_not_cancelable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sweep has no safe midpoint to abandon, so a running read finishes.

    The cancel arrives while the read is in progress -- from inside it, which is the
    deterministic way to be "during" -- and is refused, and the read completes.
    """
    store = TaskStore(_settings(tmp_path))
    refusals: list[str] = []
    real = store._dispatch

    def _cancel_midway(skill: SkillId, client: str | None) -> Any:
        try:
            store.cancel(task.task_id)
        except TaskNotCancelableError as exc:
            refusals.append(str(exc))
        return real(skill, client)

    monkeypatch.setattr(store, "_dispatch", _cancel_midway)
    task = store.submit(SkillId.FINDINGS)
    assert store.run(task.task_id).state is TaskState.COMPLETED
    assert refusals
    assert "working" in refusals[0]


def test_cancellation_over_http(tmp_path: Path) -> None:
    """200 and the cancelled task, 200 again, 409 for a finished one, 404 for a stranger."""
    settings = _settings(tmp_path, a2a_enabled=True)
    store = TaskStore(settings)
    client = FakeA2AClient(settings, store)

    waiting = store.submit(SkillId.SCAN)  # not scheduled, so still submitted
    first = client.cancel(waiting.task_id)
    assert first.status_code == 200
    assert first.json()["state"] == "canceled"
    assert client.cancel(waiting.task_id).json() == first.json()
    assert client.http.get(f"{TASKS_PATH}/{waiting.task_id}/artifact").status_code == 409

    finished = client.submit(SkillId.FINDINGS.value)
    refused = client.cancel(finished.task_id)
    assert refused.status_code == 409
    assert "cannot be cancelled" in refused.json()["detail"]

    assert client.cancel("task-nope").status_code == 404


def test_a_failed_task_is_an_outcome_rather_than_a_crash(tmp_path: Path) -> None:
    """A consumer polling a task should learn that it failed and why."""
    store = TaskStore(_settings(tmp_path).model_copy(update={"mode": RunMode.REAL}))
    task = store.execute(SkillId.SCAN, {"client": CLIENT_MINISTRY})
    assert task.state is TaskState.FAILED
    assert task.error
    assert task.artifact is None
    with pytest.raises(TaskError, match="failed"):
        store.artifact(task.task_id)


def test_the_task_states_are_only_the_reachable_ones() -> None:
    """Every member is observed by a test above; none is declared and unreachable.

    `canceled` joined in Phase 12, when submit stopped running the work inline and a
    task could be caught before it started.
    """
    assert {member.value for member in TaskState} == {
        "submitted",
        "working",
        "completed",
        "failed",
        "canceled",
    }
    assert {state for state in TaskState if state.is_terminal} == {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
    }


def test_an_unknown_task_and_an_incomplete_one_fail_differently(tmp_path: Path) -> None:
    """They lead a consumer somewhere different, so they are different errors."""
    store = TaskStore(_settings(tmp_path))
    with pytest.raises(UnknownTaskError, match="no task"):
        store.status("task-nope")
    with pytest.raises(UnknownTaskError):
        store.artifact("task-nope")
    with pytest.raises(UnknownTaskError):
        store.cancel("task-nope")


# ---------------------------------------------------------------------------
# The HTTP surface, in-process
# ---------------------------------------------------------------------------


def test_the_surface_is_off_by_default(tmp_path: Path) -> None:
    """A route another agent can reach should be something an operator turned on."""
    settings = _settings(tmp_path)
    assert not settings.a2a_enabled
    app = FastAPI()
    assert not mount(app, settings)
    assert TestClient(app).get(CARD_PATH).status_code == 404


def test_an_unadvertised_or_over_specified_request_is_refused_over_http(tmp_path: Path) -> None:
    """With the advertised list, so a consumer can correct itself; extras are a 422."""
    client = FakeA2AClient(_settings(tmp_path, a2a_enabled=True))
    refused = client.http.post(TASKS_PATH, json={"skill": "approvals.decide"})
    assert refused.status_code == 400
    assert "approvals.queue" in refused.json()["detail"]
    extra = client.http.post(TASKS_PATH, json={"skill": SkillId.SCAN.value, "action": "delete"})
    assert extra.status_code == 422
    assert client.http.get(f"{TASKS_PATH}/task-nope").status_code == 404
    assert client.http.get(f"{TASKS_PATH}/task-nope/artifact").status_code == 404


def test_the_card_path_is_where_a2a_consumers_look() -> None:
    """A card at a path nobody checks is a card nobody discovers."""
    assert CARD_PATH == "/.well-known/agent-card.json"
