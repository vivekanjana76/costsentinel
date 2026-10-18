"""The load-bearing invariant: an exemption may only make CostSentinel do *less*.

This is the one an attacker would attack. Exemption reads tags, tags are
attacker-controllable, and it uses them to change what the system does. That is only
safe in one direction.

A tag may move a finding toward "leave this alone". A tag must never move a finding
toward "act on this", must never downgrade a safety classification, and must never
make something automatable that was not.

Put plainly: the worst a hostile tag can achieve is making CostSentinel recommend
less than it should, which costs money and breaks nothing. The mirror image -- a tag
that made a deletion automatable -- is a deleted production disk. So the code is
shaped so the second one cannot be written, and this file is the evidence.

Four kinds of assertion, deliberately independent of each other:

* **structural** -- the exemption modules cannot import a classifier, and the policy
  store cannot import an exemption (``tests/test_exemption.py``);
* **representational** -- :class:`ExemptionVerdict` has no field for an action class
  or a money amount (``tests/test_exemption.py``);
* **exhaustive over the classifier** -- no key/value pair in the rule vocabulary
  moves a classification (``tests/test_exemption.py``);
* **end-to-end over the graph** -- here. A real sweep, a real destructive
  remediation, and every tag worth trying, asserting that nothing loosens and nothing
  appears.

The first three could all hold while the wiring was wrong. The fourth is what checks
the wiring.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.test_exemption import HOSTILE_VALUES
from tests.test_exemption_pipeline import (
    AS_OF,
    CLIENT,
    D4S_V5_MONTHLY,
    SUBSCRIPTION,
    _idle_vm,
    _provenance,
    _run,
    _settings,
)

from costsentinel.agents.policy_guard import make_policy_guard
from costsentinel.domain.common import MoneyAmount
from costsentinel.domain.estate import Resource, ResourceKind, ResourceState
from costsentinel.domain.recommendations import ActionClass
from costsentinel.domain.state import ScanState
from costsentinel.guardrails.exemption import ExemptionPolicy
from costsentinel.guardrails.policy import PolicyStore

#: One canonical affirmative value per built-in rule, plus every hostile string. The
#: affirmative ones *will* exempt and the hostile ones should not; neither may loosen
#: anything, which is the only claim being made.
TAGS_WORTH_TRYING: tuple[Mapping[str, str], ...] = (
    {"role": "disaster-recovery-standby"},
    {"purpose": "dr"},
    {"dr-role": "standby"},
    {"retention": "HIPAA-6YR-REF-2291"},
    {"legal-hold": "case-4417"},
    {"do-not-deallocate": "true"},
    {"always-on": "required"},
    {"cost-exempt": "true"},
    {"costsentinel-exempt": "approved"},
    *({"cost-exempt": value} for value in HOSTILE_VALUES),
    *({"role": value} for value in HOSTILE_VALUES),
    *({"retention": value} for value in HOSTILE_VALUES),
)


def _orphaned_disk(name: str, *, tags: Mapping[str, str]) -> Resource:
    """An unattached managed disk: the most destructive remediation in the system.

    Deleting one is ``review`` by floor and ``block`` in a production subscription,
    so it is the case where loosening would do the most damage.
    """
    return Resource(
        resource_id=f"/subscriptions/{SUBSCRIPTION}/disk/{name}",
        name=name,
        kind=ResourceKind.MANAGED_DISK,
        subscription_id=SUBSCRIPTION,
        resource_group="rg-continuity",
        region="eastus",
        sku="Premium_LRS_P10",
        state=ResourceState.UNATTACHED,
        tags=dict(tags),
        created_at=AS_OF - timedelta(days=400),
        monthly_cost=MoneyAmount.of(Decimal("19.71"), provenance=_provenance()),
        provenance=_provenance(),
    )


# ---------------------------------------------------------------------------
# Implemented, not merely tested
# ---------------------------------------------------------------------------


def test_the_classifier_takes_no_exemption_argument() -> None:
    """There is no parameter through which a tag could reach a classification.

    The import test says exemption cannot *see* the classifier. This says the
    classifier cannot *accept* an exemption, which closes the other way a future
    change might wire the two together.
    """
    for function in (PolicyStore.classify, PolicyStore.classification_for):
        parameters = set(inspect.signature(function).parameters)
        assert not {p for p in parameters if "exempt" in p}, function.__name__

    guard = set(inspect.signature(make_policy_guard).parameters)
    assert not {p for p in guard if "exempt" in p}


def test_the_policy_guard_classifies_from_the_action_and_the_environment_alone(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Re-running the guard over a tampered state yields the identical verdicts.

    The state handed in has every exemption stripped out. If a tag had any influence
    on a classification, removing the verdicts would change one.
    """
    state = _run(
        [_orphaned_disk("disk-orphan-01", tags={"owner": "platform"})],
        _settings(tmp_path_factory),
    )
    guard = make_policy_guard(settings=_settings(tmp_path_factory))

    stripped = state.model_copy(update={"exemptions": ()})
    again = guard(stripped)

    before = {c.recommendation_id: c for c in state.classifications}
    for classification in again["classifications"]:
        original = before[classification.recommendation_id]
        assert classification.action_class is original.action_class
        assert classification.requires_approval == original.requires_approval
        assert classification.is_automatable == original.is_automatable


# ---------------------------------------------------------------------------
# End to end, over every tag worth trying
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> ScanState:
    """The same estate with an inert tag: what every tagged run is measured against."""
    return _run(
        [
            _orphaned_disk("disk-orphan-01", tags={"owner": "platform"}),
            _idle_vm("vm-reporting-03", tags={}, monthly=D4S_V5_MONTHLY),
        ],
        _settings(tmp_path_factory),
    )


def _classes(state: ScanState) -> Mapping[str, ActionClass]:
    return {c.recommendation_id: c.action_class for c in state.classifications}


@pytest.mark.parametrize(
    "tags", TAGS_WORTH_TRYING, ids=lambda t: "|".join(f"{k}={v}" for k, v in t.items())[:48]
)
def test_no_tag_can_loosen_a_class_or_add_a_recommendation(
    tags: Mapping[str, str],
    baseline: ScanState,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The invariant, through the whole graph, for every tag worth trying.

    The disk carries the tag; the virtual machine does not. Whatever the tag does to
    the disk, the machine's advice must be untouched and nothing anywhere may become
    easier to do.
    """
    tagged = _run(
        [
            _orphaned_disk("disk-orphan-01", tags=tags),
            _idle_vm("vm-reporting-03", tags={}, monthly=D4S_V5_MONTHLY),
        ],
        _settings(tmp_path_factory),
    )

    # Nothing appeared.
    assert len(tagged.recommendations) <= len(baseline.recommendations)
    assert len(tagged.signals) == len(baseline.signals)
    assert len(tagged.classifications) <= len(baseline.classifications)

    # Nothing became easier to do.
    before = _classes(baseline)
    for recommendation_id, action_class in _classes(tagged).items():
        assert action_class.severity >= before[recommendation_id].severity, recommendation_id

    automatable = sum(1 for c in tagged.classifications if c.is_automatable)
    assert automatable <= sum(1 for c in baseline.classifications if c.is_automatable)

    gated = sum(1 for c in tagged.classifications if c.requires_approval)
    assert gated <= sum(1 for c in baseline.classifications if c.requires_approval)

    # The untagged resource's advice is byte-identical, tag or no tag.
    survivor = next(
        (r for r in tagged.recommendations if r.target_resource_name == "vm-reporting-03"), None
    )
    assert survivor is not None
    original = next(
        r
        for r in baseline.recommendations
        if r.target_resource_name == survivor.target_resource_name
    )
    assert survivor.action is original.action
    assert survivor.risk_class is original.risk_class
    assert survivor.savings.monthly.amount == original.savings.monthly.amount


@pytest.mark.parametrize(
    "tags", TAGS_WORTH_TRYING, ids=lambda t: "|".join(f"{k}={v}" for k, v in t.items())[:48]
)
def test_a_destructive_delete_that_survives_still_needs_a_human(
    tags: Mapping[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Either the deletion is withheld, or it is gated. There is no third outcome.

    This is the assertion that would catch the failure mode that matters: a tag that
    left the recommendation in place but relaxed its gate.
    """
    tagged = _run([_orphaned_disk("disk-orphan-01", tags=tags)], _settings(tmp_path_factory))
    for classification in tagged.classifications:
        assert classification.action_class is not ActionClass.ALLOW, tags
        assert classification.requires_approval, tags


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _order(state: ScanState) -> Sequence[str]:
    return [r.target_resource_name for r in sorted(state.recommendations, key=lambda r: r.rank)]


def test_exemption_renumbers_ranks_and_never_reorders_them(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A removed item above you lowers your number. Nothing raises it.

    Ranks are positions in a list, so dropping an entry renumbers what is left --
    there is no way to exempt something and leave the remaining numbers untouched,
    and pretending otherwise would mean leaving a gap where a proposal used to be.
    What must hold is that the *relative* order is unchanged and no finding moves
    down the page.

    The estate is built so that the exempt resource holds the largest saving, which
    is the case where this could plausibly fail: the savings component is normalised
    against the largest saving in the run, so removing the leader rescales that
    component for everything else while leaving the safety and confidence components
    alone. That is not a monotone transform of the composite, so the order genuinely
    could move. It does not, and this is where we find out if that changes.
    """
    resources = [
        _idle_vm("vm-biggest", tags={"owner": "a"}),  # D8s_v5, the largest saving
        _idle_vm("vm-middle", tags={"owner": "b"}, monthly=D4S_V5_MONTHLY),
        _orphaned_disk("disk-orphan-01", tags={"owner": "c"}),
    ]
    without = _run(resources, _settings(tmp_path_factory))
    with_tag = _run(
        [
            _idle_vm("vm-biggest", tags={"role": "disaster-recovery-standby"}),
            *resources[1:],
        ],
        _settings(tmp_path_factory),
    )

    assert _order(without)[0] == "vm-biggest"
    assert "vm-biggest" not in _order(with_tag)

    # Relative order of the survivors is preserved...
    survivors = [name for name in _order(without) if name != "vm-biggest"]
    assert _order(with_tag) == survivors

    # ...and no survivor's rank got worse.
    before = {r.target_resource_name: r.rank for r in without.recommendations}
    for recommendation in with_tag.recommendations:
        assert recommendation.rank <= before[recommendation.target_resource_name]


def test_an_exempt_resource_never_appears_in_the_approval_queue(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Nothing is ever put to a human about a resource nobody is proposing to touch."""
    state = _run(
        [_orphaned_disk("disk-orphan-01", tags={"retention": "HIPAA-6YR-REF-2291"})],
        _settings(tmp_path_factory),
    )
    assert state.approval_requests == ()
    assert state.report is not None
    assert state.report.approval_queue == ()
    assert len(state.report.exempt_findings) == 1


def test_the_builtin_rules_cannot_exempt_an_untagged_estate() -> None:
    """The guard against a rule whose key set is so broad it catches everything."""
    policy = ExemptionPolicy.builtin()
    for tags in ({}, {"owner": "x"}, {"env": "prod"}, {"cost-centre": "4417"}):
        verdict = policy.verdict(resource_id="/r", resource_name="r", tags=tags, client=CLIENT)
        assert not verdict.is_exempt, tags
