"""Agent Card signing: a JWS per A2A v1 section 8.4, and a labelled HMAC fallback.

The JWS (the A2A v1 signature)
------------------------------

Spec section 8.4.2, step by step, and nothing added:

1. **Payload**: the served card with ``signatures`` excluded and absent fields omitted,
   canonicalised per RFC 8785. See :meth:`~costsentinel.a2a.card.AgentCard.canonical`.
2. **Protected header**: ``{"alg": "ES256", "typ": "JOSE", "kid": ..., "jku": ...}``.
   ``kid`` is the key's RFC 7638 thumbprint, so it is derived rather than chosen, and a
   key cannot be published under a second name. ``jku`` points at the JWKS this
   server publishes.
3. **Signature**: ES256 over ``BASE64URL(header) || "." || BASE64URL(payload)``,
   encoded as raw ``r || s`` per RFC 7518 section 3.4. That is not DER, which is what
   the crypto library emits and the classic way an ES256 implementation goes wrong.
4. **Detached**: the card carries ``protected`` and ``signature``, and the verifier
   rebuilds the payload from the card it received (RFC 7515 Appendix F).

Verification here refuses any ``alg`` other than ES256. ``none`` and the HMAC family are
the textbook algorithm-confusion attacks, and the fallback below is not a JWS for
exactly that reason.

**What is and is not claimed.** The signature proves the card was not modified since a
holder of the private key signed it. It does not prove *who* holds that key. ``jku`` is
served by the same origin as the card, so fetching the public key over it adds nothing
TLS did not already give. A consumer who wants provenance pins the key out of band.
That is spec 8.4.3's "trusted key store", and it is the only reading under which this
signature means more than integrity.

Keys
----

- ``A2A_JWS_KEY_FILE``: a PEM P-256 private key. It signs the card in any mode.
- **Mock mode with no file**: a synthetic key *derived* from a labelled constant. No
  private key is committed, which ``detect-private-key`` would rightly refuse, and CI
  still signs and verifies on every run. It signs nothing anybody should trust.
- **Real mode with no file**: an unsigned card. A signature anybody can forge is worse
  than none, because it invites a consumer to trust it.

The detached HMAC (a fallback, and not an A2A signature)
---------------------------------------------------------

Phase 10 shipped a detached HMAC-SHA256 and some deployments may already check it. It
stays as an opt-in fallback for those, when ``A2A_SIGNING_KEY`` is set. It is served
**out of band** in the ``X-CostSentinel-Card-HMAC`` response header, never in
``signatures``, because it is not a JWS and an A2A client must never mistake it for one.
It covers the same RFC 8785 payload. It is symmetric: anybody able to verify it can
also forge it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from costsentinel.a2a.card import AgentCard, AgentCardSignature, Json, canonical_payload

#: The one JWS algorithm this agent signs with and accepts.
ALGORITHM = "ES256"

#: Where the public key is published, so ``jku`` has something to point at.
JWKS_PATH = "/.well-known/agent-card-jwks.json"

#: The response header that carries the fallback HMAC. Named so nobody reads it as A2A.
HMAC_HEADER = "X-CostSentinel-Card-HMAC"

#: The seed the synthetic key is derived from. Committed deliberately, and worthless
#: deliberately: the key it yields signs nothing anybody should trust, and a real
#: deployment supplies its own through A2A_JWS_KEY_FILE.
SYNTHETIC_KEY_SEED = "costsentinel-synthetic-card-key-not-a-secret"

#: P-256's group order. A derived private scalar must lie in [1, n - 1].
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

#: Bytes in one P-256 coordinate, and in each half of a raw ES256 signature.
_COORDINATE = 32


class _ProtectedHeader(BaseModel):
    """The protected header as received. Untrusted, so parsed rather than probed."""

    model_config = ConfigDict(extra="allow")

    alg: str
    kid: str


_SIGNATURES: TypeAdapter[tuple[AgentCardSignature, ...]] = TypeAdapter(
    tuple[AgentCardSignature, ...]
)


class CardSigningError(ValueError):
    """A signing key could not be loaded or is not a P-256 key."""


def b64url(data: bytes) -> str:
    """Base64url without padding, as JOSE uses throughout."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """The inverse of :func:`b64url`, tolerant of the missing padding."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def synthetic_key() -> ec.EllipticCurvePrivateKey:
    """The mock-mode key, derived from :data:`SYNTHETIC_KEY_SEED`. Deterministic."""
    digest = int.from_bytes(hashlib.sha256(SYNTHETIC_KEY_SEED.encode()).digest(), "big")
    return ec.derive_private_key(digest % (_P256_ORDER - 1) + 1, ec.SECP256R1())


def load_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """A PEM private key from disk, which must be P-256.

    Raises:
        CardSigningError: If the file is missing, unreadable or the wrong kind of key.
            The message says which, because a misconfigured key otherwise surfaces as
            an unsigned card that nobody notices.
    """
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        msg = f"A2A_JWS_KEY_FILE={str(path)!r} could not be loaded as an unencrypted PEM key"
        raise CardSigningError(msg) from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        msg = f"A2A_JWS_KEY_FILE={str(path)!r} must hold a P-256 (ES256) private key"
        raise CardSigningError(msg)
    return key


def public_jwk(key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    """The public key as a JWK (RFC 7517), with its thumbprint as ``kid``."""
    numbers = key.public_numbers()
    members = {
        "crv": "P-256",
        "kty": "EC",
        "x": b64url(numbers.x.to_bytes(_COORDINATE, "big")),
        "y": b64url(numbers.y.to_bytes(_COORDINATE, "big")),
    }
    return {**members, "kid": thumbprint(members), "use": "sig", "alg": ALGORITHM}


def thumbprint(members: Mapping[str, str]) -> str:
    """RFC 7638: SHA-256 over the required members, sorted and without whitespace."""
    required = {name: members[name] for name in ("crv", "kty", "x", "y")}
    encoded = json.dumps(required, sort_keys=True, separators=(",", ":")).encode()
    return b64url(hashlib.sha256(encoded).digest())


def jwks(key: ec.EllipticCurvePrivateKey) -> dict[str, Any]:
    """The JWKS ``jku`` points at. Public key only, by construction."""
    return {"keys": [public_jwk(key.public_key())]}


def sign_card(card: AgentCard, key: ec.EllipticCurvePrivateKey, *, jku: str) -> AgentCard:
    """Return the card carrying one JWS over its canonical form, per spec 8.4.2.

    Any existing signatures are replaced, not appended to. A card is signed once by
    the key that serves it, and a stale signature over different bytes left beside a
    fresh one would only fail.
    """
    unsigned = card.model_copy(update={"signatures": ()})
    header = {
        "alg": ALGORITHM,
        "typ": "JOSE",
        "kid": public_jwk(key.public_key())["kid"],
        "jku": jku,
    }
    protected = b64url(json.dumps(header, separators=(",", ":")).encode())
    signing_input = f"{protected}.{b64url(unsigned.canonical())}".encode("ascii")
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    raw = r.to_bytes(_COORDINATE, "big") + s.to_bytes(_COORDINATE, "big")
    signature = AgentCardSignature(protected=protected, signature=b64url(raw))
    return unsigned.model_copy(update={"signatures": (signature,)})


def verify_card(served: Mapping[str, Json], key: ec.EllipticCurvePublicKey) -> bool:
    """Whether any signature on a served card verifies under ``key``. Spec 8.4.3.

    Takes the card as received JSON rather than as a model, so the payload is rebuilt
    from the bytes on the wire (see :func:`~costsentinel.a2a.card.canonical_payload`).
    An unsigned card does not verify. Vacuous success would make "unsigned" and
    "signed" indistinguishable to a caller who checks the boolean.
    """
    try:
        signatures = _SIGNATURES.validate_python(served.get("signatures") or ())
    except ValidationError:
        return False
    payload = b64url(canonical_payload(served))
    expected_kid = public_jwk(key)["kid"]
    return any(_verify_one(entry, payload, key, expected_kid) for entry in signatures)


def _verify_one(
    entry: AgentCardSignature, payload: str, key: ec.EllipticCurvePublicKey, kid: str
) -> bool:
    try:
        header = _ProtectedHeader.model_validate_json(b64url_decode(entry.protected))
        raw = b64url_decode(entry.signature)
    except (ValueError, ValidationError):
        return False
    if header.alg != ALGORITHM or header.kid != kid:
        return False
    if len(raw) != 2 * _COORDINATE:
        return False
    der = encode_dss_signature(
        int.from_bytes(raw[:_COORDINATE], "big"), int.from_bytes(raw[_COORDINATE:], "big")
    )
    try:
        key.verify(der, f"{entry.protected}.{payload}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def card_hmac(served: Mapping[str, Json], secret: str) -> str:
    """The fallback: ``sha256=<hex>`` over the same RFC 8785 payload. Not A2A v1."""
    digest = hmac.new(secret.encode(), canonical_payload(served), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_card_hmac(served: Mapping[str, Json], secret: str, header: str) -> bool:
    """Constant-time check of the fallback header. An empty header never verifies."""
    if not header:
        return False
    return hmac.compare_digest(card_hmac(served, secret), header)
