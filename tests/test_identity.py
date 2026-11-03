"""The one identity and authorisation path (ARCHITECTURE.md D132).

Every surface that can decide asks :func:`authorise` the same question. These tests pin
its answers, and above all its refusals, because a permissive default here would be a
permission nobody granted.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from costsentinel.approvals.identity import (
    AllowEntry,
    ApproverIdentity,
    AuthorisationPolicy,
    authorise,
)
from costsentinel.config import Settings
from costsentinel.domain.governance import ApproverAssurance

ALICE = "slack:T0SYNTH01/U0ALICE01"
OPS_GROUP = "slack-group:T0SYNTH01/S0FINOPS1"


def _verified(principal: str = ALICE, *, groups: tuple[str, ...] = ()) -> ApproverIdentity:
    return ApproverIdentity(
        name="alice",
        assurance=ApproverAssurance.VERIFIED,
        verified_by="slack-signature",
        principal=principal,
        groups=groups,
    )


# ---------------------------------------------------------------------------
# What an identity may claim
# ---------------------------------------------------------------------------


def test_a_verified_identity_must_name_what_was_verified() -> None:
    """A "verified" record with no principal would vouch for nobody in particular."""
    with pytest.raises(ValidationError, match="principal"):
        ApproverIdentity(
            name="alice", assurance=ApproverAssurance.VERIFIED, verified_by="slack-signature"
        )


def test_a_self_asserted_identity_cannot_carry_a_principal() -> None:
    """A typed name with a principal attached would read as half-verified."""
    with pytest.raises(ValidationError, match="vouched for by nothing"):
        ApproverIdentity(name="alice", assurance=ApproverAssurance.SELF_ASSERTED, principal=ALICE)


@pytest.mark.parametrize(
    "principal",
    ["alice", "slack:T1/U1,token:x", "slack:T1/U1@client", "ldap:alice", "slack:", "token:a b"],
)
def test_a_malformed_principal_is_refused(principal: str) -> None:
    """A principal is matched against an allowlist, so it may not contain a separator."""
    with pytest.raises(ValidationError):
        _verified(principal)


# ---------------------------------------------------------------------------
# Reading the allowlist
# ---------------------------------------------------------------------------


def test_the_allowlist_is_empty_by_default() -> None:
    """Nobody is an approver until somebody names them."""
    assert not AuthorisationPolicy.from_settings(Settings()).configured


def test_the_allowlist_reads_principals_and_client_scopes() -> None:
    """The documented syntax, including whitespace a human would type."""
    policy = AuthorisationPolicy.parse(f" {ALICE} , {OPS_GROUP}@northwind-energy ,, ")
    assert policy.entries == (
        AllowEntry(principal=ALICE),
        AllowEntry(principal=OPS_GROUP, client="northwind-energy"),
    )


@pytest.mark.parametrize("raw", ["alice@example.com", "slack:T1/U1, nonsense", "admin"])
def test_a_typo_in_the_allowlist_stops_the_process(raw: str) -> None:
    """A malformed entry must fail loudly, not silently admit nobody or the wrong person."""
    with pytest.raises(ValidationError):
        AuthorisationPolicy.parse(raw)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_a_self_asserted_name_is_accepted_on_a_local_surface() -> None:
    """The pre-10b behaviour of the CLI and a loopback dashboard, unchanged."""
    verdict = authorise(
        ApproverIdentity.self_asserted("ops"),
        client="northwind-energy",
        local=True,
        policy=AuthorisationPolicy(),
    )
    assert verdict.allowed


def test_a_self_asserted_name_from_elsewhere_is_refused_whatever_the_allowlist() -> None:
    """Typing somebody's name off this machine must never be enough."""
    policy = AuthorisationPolicy.parse("token:ops")
    verdict = authorise(
        ApproverIdentity.self_asserted("ops"), client="northwind-energy", local=False, policy=policy
    )
    assert not verdict.allowed
    assert "only on this machine" in verdict.reason


def test_a_verified_identity_with_no_allowlist_is_refused() -> None:
    """Fail closed: being in the Slack workspace is not a permission to approve."""
    verdict = authorise(
        _verified(), client="northwind-energy", local=False, policy=AuthorisationPolicy()
    )
    assert not verdict.allowed
    assert "APPROVER_ALLOWLIST" in verdict.reason


def test_an_allowlisted_principal_is_admitted_and_the_match_is_named() -> None:
    """The record should say which allowlist entry let this decision through."""
    verdict = authorise(
        _verified(), client="northwind-energy", local=False, policy=AuthorisationPolicy.parse(ALICE)
    )
    assert verdict.allowed
    assert verdict.matched == ALICE


def test_membership_of_an_allowlisted_group_admits() -> None:
    """A FinOps group is how most teams will name their approvers."""
    verdict = authorise(
        _verified("slack:T0SYNTH01/U0BOB0001", groups=(OPS_GROUP,)),
        client="northwind-energy",
        local=False,
        policy=AuthorisationPolicy.parse(OPS_GROUP),
    )
    assert verdict.allowed
    assert verdict.matched == OPS_GROUP


def test_a_client_scoped_entry_admits_only_for_that_client() -> None:
    """One client's FinOps lead must not be able to approve another client's deletion."""
    policy = AuthorisationPolicy.parse(f"{ALICE}@northwind-energy")
    assert authorise(_verified(), client="northwind-energy", local=False, policy=policy).allowed
    refused = authorise(_verified(), client="ministry-of-transport", local=False, policy=policy)
    assert not refused.allowed
    assert "ministry-of-transport" in refused.reason


def test_an_unlisted_principal_is_refused_even_when_others_are_listed() -> None:
    """The ordinary refusal: verified, and simply not an approver."""
    verdict = authorise(
        _verified("slack:T0SYNTH01/U0MALLORY"),
        client="northwind-energy",
        local=False,
        policy=AuthorisationPolicy.parse(ALICE),
    )
    assert not verdict.allowed
    assert "not on the approver allowlist" in verdict.reason


def test_a_verified_identity_on_a_local_surface_still_needs_the_allowlist() -> None:
    """Locality excuses an unverified name; it does not widen what a verified one may do."""
    verdict = authorise(
        _verified("slack:T0SYNTH01/U0MALLORY"),
        client="northwind-energy",
        local=True,
        policy=AuthorisationPolicy.parse(ALICE),
    )
    assert not verdict.allowed
