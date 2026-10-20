"""The Agent Card, the task lifecycle, and the claim that the surface cannot write.

The first real A2A surface: one advertised agent, three read-only skills, a signed
card and a submit/status/artifact lifecycle. Exercised entirely in-process -- the
FastAPI test client speaks to the app directly and nothing opens a socket, which is
how CI runs it.

The load-bearing test here is `test_the_advertised_skills_contain_no_write_capability`.
It matches skill ids, names, descriptions and tags against a vocabulary of mutating
verbs rather than against an allowed list, because the failure to guard against is
somebody adding `remediation.apply` later and nobody noticing the card now advertises
a write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from costsentinel.a2a.card import (
    PROTOCOL_VERSION,
    READ_ONLY_SKILLS,
    SYNTHETIC_SIGNING_KEY,
    AgentCard,
    SkillId,
    build_card,
)
from costsentinel.a2a.server import CARD_PATH, build_router, card_for, mount, signing_key
from costsentinel.a2a.tasks import (
    TaskError,
    TaskState,
    TaskStore,
    UnknownSkillError,
    UnknownTaskError,
)
from costsentinel.config import LLMBackend, MemoryBackend, RunMode, Settings
from costsentinel.providers.mock import CLIENT_MINISTRY

#: Verbs that would mean the surface can change something. Matched case-insensitively
#: against every string a consumer reads off the card.
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


def _settings(scratch: Path, *, a2a_enabled: bool = False) -> Settings:
    """Offline settings, with the A2A surface off unless a test turns it on."""
    return Settings(
        mode=RunMode.MOCK,
        llm_backend=LLMBackend.FAKE,
        memory_backend=MemoryBackend.OFF,
        state_dir=scratch,
        a2a_enabled=a2a_enabled,
    )


# ---------------------------------------------------------------------------
# The card advertises nothing that writes
# ---------------------------------------------------------------------------


def test_the_advertised_skills_contain_no_write_capability() -> None:
    """The load-bearing claim of the A2A surface.

    Matched against a vocabulary rather than an allowed list, so a skill added later
    has to pass this rather than merely not being on a list somebody forgot to update.
    Every string a consuming agent reads is checked -- the id, the name, the
    description and the tags -- because a skill called `report.findings` whose
    description offered to apply them would advertise a write just as effectively.
    """
    for skill in READ_ONLY_SKILLS:
        haystacks = {
            "id": skill.id.value,
            "name": skill.name,
            "tags": " ".join(skill.tags),
            "examples": " ".join(skill.examples),
        }
        for where, text in haystacks.items():
            lowered = text.lower()
            offending = [verb for verb in MUTATING_VOCABULARY if verb in lowered]
            assert not offending, f"{skill.id.value} {where}: {offending}"


def test_a_skill_description_may_only_mention_a_mutation_to_deny_it() -> None:
    """The descriptions are prose, so a blanket ban would be the wrong check.

    "has no path to change a resource" is the most useful sentence on the card and it
    contains "change". So a description may use a mutating word only in a sentence that
    denies the capability, and the denial is what is asserted -- every description says
    it reads, and the queue skill says explicitly that it cannot decide.
    """
    for skill in READ_ONLY_SKILLS:
        lowered = skill.description.lower()
        assert "read" in lowered, f"{skill.id.value} does not say it reads"

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


def test_the_card_declares_itself_read_only(tmp_path: Path) -> None:
    """Stated on the card so a consumer need not infer it from the skill list."""
    card = card_for(_settings(tmp_path))
    assert card.read_only
    assert card.protocol_version == PROTOCOL_VERSION
    assert "no path to change a resource" in card.description


def test_an_external_caller_cannot_reach_a_decision_or_an_execution(tmp_path: Path) -> None:
    """The A2A equivalent of the four no-mutation proofs.

    There is no advertised skill that decides, and the task store has no branch that
    could run one: asking for anything unadvertised fails with the list of what is.
    """
    store = TaskStore(_settings(tmp_path))

    for attempt in ("approvals.decide", "approvals.approve", "remediation.apply", "execute"):
        with pytest.raises(UnknownSkillError, match="not an advertised skill"):
            store.submit(attempt)

    assert store.count == 0, "a refused skill must not create a task"


def test_no_route_on_the_surface_mutates_anything(tmp_path: Path) -> None:
    """Asserted against the routes, not the docstrings.

    One POST, which creates a *task*, and every task it can create is a read. If a
    second POST or any PUT, PATCH or DELETE appeared, this is where it would show.
    """
    router = build_router(_settings(tmp_path))
    methods: dict[str, set[str]] = {}
    for route in router.routes:
        path = getattr(route, "path", "")
        methods.setdefault(path, set()).update(getattr(route, "methods", set()) or set())

    writing = {path: verbs & {"PUT", "PATCH", "DELETE"} for path, verbs in methods.items()}
    assert not any(writing.values()), writing

    posts = [path for path, verbs in methods.items() if "POST" in verbs]
    assert posts == ["/a2a/tasks"], posts


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_a_signed_card_verifies_and_a_tampered_one_does_not(tmp_path: Path) -> None:
    """The point of signing: a modified card fails, including a modified skill list.

    The third case is the one that matters. Somebody adding a write capability in
    transit should not be able to leave the signature intact.
    """
    card = card_for(_settings(tmp_path))
    assert card.signature
    assert card.verify(SYNTHETIC_SIGNING_KEY)
    assert not card.verify("some-other-key")

    renamed = card.model_copy(update={"description": "now with execution"})
    assert not renamed.verify(SYNTHETIC_SIGNING_KEY)

    extra_skill = card.model_copy(
        update={"skills": (*card.skills, card.skills[0].model_copy(update={"name": "Apply"}))}
    )
    assert not extra_skill.verify(SYNTHETIC_SIGNING_KEY)


def test_an_unsigned_card_does_not_verify_trivially() -> None:
    """An empty signature must fail, not pass by vacuity."""
    card = build_card(url="http://localhost:8000", version="0.0.0")
    assert card.signature == ""
    assert not card.verify(SYNTHETIC_SIGNING_KEY)
    assert not card.verify("")


def test_the_canonical_bytes_exclude_the_signature(tmp_path: Path) -> None:
    """Otherwise signing would change what the signature covers.

    Also sorted and whitespace-free, so a consumer can recompute it without matching
    our serialiser's key order.
    """
    card = card_for(_settings(tmp_path))
    assert b'"signature"' not in card.canonical()
    payload = json.loads(card.canonical())
    assert list(payload) == sorted(payload)
    assert card.canonical() == card.model_copy(update={"signature": "x" * 64}).canonical()


def test_real_mode_without_a_key_produces_an_unsigned_card(tmp_path: Path) -> None:
    """A signature anybody can forge is worse than none: it invites trust.

    So the committed synthetic key is used in mock mode -- where it lets CI check that
    signing works at all -- and never stands in for a real one.
    """
    assert signing_key(_settings(tmp_path)) == SYNTHETIC_SIGNING_KEY

    real = _settings(tmp_path).model_copy(update={"mode": RunMode.REAL})
    assert signing_key(real) == ""
    assert not card_for(real).signature


def test_the_synthetic_key_is_labelled_as_worthless() -> None:
    """A committed key that reads like a secret is one somebody will treat as one."""
    assert "not-a-secret" in SYNTHETIC_SIGNING_KEY
    assert "synthetic" in SYNTHETIC_SIGNING_KEY


# ---------------------------------------------------------------------------
# The task lifecycle
# ---------------------------------------------------------------------------


def test_a_scan_task_returns_a_report_as_its_artifact(tmp_path: Path) -> None:
    """Submit, finished, artefact. The lifecycle a consumer codes against."""
    store = TaskStore(_settings(tmp_path))
    task = store.submit(SkillId.SCAN, {"client": CLIENT_MINISTRY})

    assert task.state is TaskState.COMPLETED
    assert task.is_finished
    assert task.error == ""
    assert store.status(task.task_id) == task

    artifact = store.artifact(task.task_id)
    assert artifact.name == "cost-review"
    assert artifact.media_type == "application/json"
    reports = artifact.payload["reports"]
    assert len(reports) == 1
    assert reports[0]["client"] == CLIENT_MINISTRY
    assert reports[0]["findings"]
    assert "no execution path" in str(artifact.payload["note"])


def test_the_queue_skill_reports_what_a_paused_sweep_is_waiting_on(tmp_path: Path) -> None:
    """The capability the card came forward for, read through the A2A surface."""
    settings = _settings(tmp_path)
    store = TaskStore(settings)

    scanned = store.submit(SkillId.SCAN, {"client": CLIENT_MINISTRY})
    assert scanned.state is TaskState.COMPLETED

    queued = store.submit(SkillId.APPROVAL_QUEUE, {"client": CLIENT_MINISTRY})
    payload = store.artifact(queued.task_id).payload
    assert payload["awaiting"] == 2
    assert payload["sweeps"]
    first = payload["sweeps"][0]
    assert first["client"] == CLIENT_MINISTRY
    assert len(first["pending"]) == 2
    # And it says who decides, since it is not this surface.
    assert "approvals decide" in str(payload["note"])
    assert "no path to decide it" in str(payload["note"])


def test_the_findings_skill_reads_stored_results_rather_than_rescanning(
    tmp_path: Path,
) -> None:
    """The current findings are what was last reported, not what a rescan would say.

    Those are different answers, and a consumer asking this skill wants the first --
    so it reads the checkpoint rather than running a sweep.
    """
    settings = _settings(tmp_path)
    store = TaskStore(settings)

    assert store.artifact(store.submit(SkillId.FINDINGS).task_id).payload["sweeps"] == 0

    store.submit(SkillId.SCAN, {"client": CLIENT_MINISTRY})
    after = store.artifact(store.submit(SkillId.FINDINGS).task_id).payload
    assert after["sweeps"] == 1
    assert after["reports"][0]["client"] == CLIENT_MINISTRY
    assert "No cloud call and no model call" in str(after["note"])


def test_an_unknown_task_and_an_incomplete_one_fail_differently(tmp_path: Path) -> None:
    """They lead a consumer somewhere different, so they are different errors."""
    store = TaskStore(_settings(tmp_path))
    with pytest.raises(UnknownTaskError, match=r"never issued|no task"):
        store.status("task-nope")
    with pytest.raises(UnknownTaskError):
        store.artifact("task-nope")


def test_a_failed_task_is_an_outcome_rather_than_a_crash(tmp_path: Path) -> None:
    """A consumer polling a task should learn that it failed and why.

    Raising out of `submit` would make a failed read indistinguishable from a broken
    agent, and the lifecycle exists precisely so a caller can be told.
    """
    settings = _settings(tmp_path).model_copy(update={"mode": RunMode.REAL})
    store = TaskStore(settings)
    task = store.submit(SkillId.SCAN, {"client": CLIENT_MINISTRY})

    assert task.state is TaskState.FAILED
    assert task.error
    assert task.artifact is None
    with pytest.raises(TaskError, match="failed"):
        store.artifact(task.task_id)


def test_the_task_states_are_only_the_reachable_ones() -> None:
    """Declaring a state a consumer can never observe would misdescribe the surface."""
    assert {member.value for member in TaskState} == {
        "submitted",
        "working",
        "completed",
        "failed",
    }


# ---------------------------------------------------------------------------
# The HTTP surface, in-process
# ---------------------------------------------------------------------------


def test_the_surface_is_off_by_default(tmp_path: Path) -> None:
    """A route another agent can reach should be something an operator turned on."""
    from fastapi import FastAPI

    settings = _settings(tmp_path)
    assert not settings.a2a_enabled

    app = FastAPI()
    assert not mount(app, settings)
    assert TestClient(app).get(CARD_PATH).status_code == 404


def test_the_enabled_surface_serves_the_card_and_the_lifecycle(tmp_path: Path) -> None:
    """End to end over HTTP, in-process: no socket, no network, no credential."""
    from fastapi import FastAPI

    settings = _settings(tmp_path, a2a_enabled=True)
    app = FastAPI()
    assert mount(app, settings, TaskStore(settings))
    client = TestClient(app)

    served = client.get(CARD_PATH)
    assert served.status_code == 200
    card = AgentCard.model_validate(served.json())
    assert card.verify(SYNTHETIC_SIGNING_KEY)
    assert card.read_only
    assert {skill.id for skill in card.skills} == set(SkillId)

    created = client.post(
        "/a2a/tasks", json={"skill": SkillId.SCAN.value, "client": CLIENT_MINISTRY}
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    assert client.get(f"/a2a/tasks/{task_id}").json()["state"] == "completed"
    artifact = client.get(f"/a2a/tasks/{task_id}/artifact")
    assert artifact.status_code == 200
    assert artifact.json()["payload"]["reports"][0]["client"] == CLIENT_MINISTRY


def test_an_unadvertised_skill_is_refused_over_http(tmp_path: Path) -> None:
    """With the advertised list, so a consumer can correct itself."""
    from fastapi import FastAPI

    settings = _settings(tmp_path, a2a_enabled=True)
    app = FastAPI()
    mount(app, settings, TaskStore(settings))
    client = TestClient(app)

    refused = client.post("/a2a/tasks", json={"skill": "approvals.decide"})
    assert refused.status_code == 400
    assert "approvals.queue" in refused.json()["detail"]

    # And an over-specified request is refused outright rather than partly honoured.
    assert (
        client.post(
            "/a2a/tasks", json={"skill": SkillId.SCAN.value, "action": "delete"}
        ).status_code
        == 422
    )


def test_an_unknown_task_is_a_404_and_an_incomplete_one_a_409(tmp_path: Path) -> None:
    from fastapi import FastAPI

    settings = _settings(tmp_path, a2a_enabled=True)
    app = FastAPI()
    mount(app, settings, TaskStore(settings))
    client = TestClient(app)

    assert client.get("/a2a/tasks/task-nope").status_code == 404
    assert client.get("/a2a/tasks/task-nope/artifact").status_code == 404


def test_the_card_path_is_where_a2a_consumers_look() -> None:
    """A card at a path nobody checks is a card nobody discovers."""
    assert CARD_PATH == "/.well-known/agent-card.json"


def test_the_a2a_package_imports_nothing_that_could_act() -> None:
    """The same structural check the approval gate gets, for the same reason.

    An external surface is the one place where "there is no write path" has to be true
    of code somebody else can reach.
    """
    import ast

    imported: set[str] = set()
    for path in sorted(Path("src/costsentinel/a2a").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

    assert not any(name.startswith("azure") for name in imported), imported
    assert not any("mgmt" in name for name in imported), imported
