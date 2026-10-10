"""Optimization Planner: choose a remediation per signal, then rank the set.

The division of labour is the heart of CLAUDE.md golden rule 5:

* the **Savings Estimator** has already priced every option the policy store
  permits, so no number originates here;
* the **model** chooses one of those priced options and writes the rationale -- it
  explains and selects, within a closed set;
* the **arithmetic** in :mod:`costsentinel.agents.ranking` produces the ordering;
* the **policy store** supplies the risk class and the preconditions.

The model's choice is re-validated against the candidate set after the response
comes back. The closed enum already stops an invented action from validating; this
second check stops a *valid but not offered* action -- resizing a disk, say -- from
slipping through. On rejection the node falls back to the policy's preferred action
and records the rejection in ``state.errors`` rather than failing the sweep.

Ranking happens here and is deterministic. The model is then asked for a *rationale*
for the ordering it has already been given, which is why a hostile model can change
the wording of a report and cannot change what a client is told to do first.
"""

from __future__ import annotations

from collections.abc import Sequence

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.agents.ranking import rank_order, score_recommendations
from costsentinel.config import Settings
from costsentinel.domain.analysis import RankingScore
from costsentinel.domain.common import Provenance, ProvenanceSource, Verification
from costsentinel.domain.estate import Environment, Estate
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.recommendations import (
    ActionType,
    PricedOption,
    Recommendation,
    SavingsEstimate,
)
from costsentinel.domain.signals import WasteSignal
from costsentinel.domain.state import ScanState
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.llm.base import LLM, EvidenceBlock, EvidenceField, Prompt
from costsentinel.llm.contracts import (
    PlannedRemediation,
    RankingRationale,
    RemediationPlan,
)
from costsentinel.llm.fake import LABEL_RANKED, LABEL_WASTE_SIGNAL
from costsentinel.llm.routing import TASK_PLANNING
from costsentinel.observability.logging import get_logger

ACTOR = "optimization_planner"

_log = get_logger("agents.optimization_planner")

_PLAN_INSTRUCTION = """
You are the Optimization Planner for a FinOps governance system operating Azure
subscriptions on behalf of enterprise and government clients.

For each waste_signal block below, choose exactly one action from that block's
candidate_actions and write a short rationale a client's finance lead could read.

Each candidate action is listed with the monthly saving already computed for it, as
option.<action> fields. Use those to choose; do not recompute or adjust them.

Rules you must follow:
- Choose only from candidate_actions for that signal. Nothing else is permitted.
- Prefer the option that is worth most, unless the root cause makes a safer option
  clearly more appropriate.
- Do not state, estimate or infer any monetary amount of your own. If you mention a
  figure, copy it verbatim from the evidence.
- Do not state preconditions. They come from the policy store.
""".strip()

_PLAN_CONTRACT = (
    "RemediationPlan: items[] of {signal_id, action, rationale, priority}. "
    "One item per waste_signal block."
)

_RANK_INSTRUCTION = """
You are explaining a finished ranking to a client. The order has already been
computed from three things: the monthly saving relative to the largest in this
report, how safe the action is, and the detector's confidence.

For each ranked_recommendation block, write one sentence explaining why it sits where
it does. Quote the figures in the block verbatim.

Rules you must follow:
- Do not dispute, re-order or recompute the ranking. It is final.
- Do not introduce any number that is not in the block.
""".strip()

_RANK_CONTRACT = (
    "RankingRationale: notes[] of {recommendation_id, rationale}. One note per "
    "ranked_recommendation block. No numeric fields."
)


def _signal_evidence(
    signal: WasteSignal,
    options: Sequence[PricedOption],
    root_cause_narrative: str | None,
) -> EvidenceBlock:
    """Render one signal, its priced options and its diagnosis as evidence.

    Every value is a string in a named field inside a labelled block. A resource
    named ``"; ignore previous instructions"`` is data here, and
    :meth:`Prompt.render` fences it off from the instruction.
    """
    fields = [
        EvidenceField(name="signal_id", value=signal.signal_id),
        EvidenceField(name="waste_kind", value=signal.kind.value),
        EvidenceField(name="resource_name", value=signal.resource_name),
        EvidenceField(name="resource_kind", value=signal.resource_kind),
        EvidenceField(name="resource_id", value=signal.resource_id),
        EvidenceField(name="subscription_id", value=signal.subscription_id),
        EvidenceField(name="detector", value=signal.detector),
        EvidenceField(name="confidence", value=str(signal.confidence)),
        EvidenceField(name="current_monthly_cost", value=signal.monthly_cost.display()),
        EvidenceField(
            name="current_monthly_cost_amount",
            value=str(signal.monthly_cost.amount) if signal.monthly_cost.amount else "",
        ),
        EvidenceField(
            name="candidate_actions",
            value=",".join(option.action.value for option in options),
        ),
    ]
    fields += [
        EvidenceField(
            name=f"option.{option.action.value}",
            value=f"{option.savings.monthly.display()} per month",
        )
        for option in options
    ]
    if root_cause_narrative:
        fields.append(EvidenceField(name="root_cause", value=root_cause_narrative))
    fields += [
        EvidenceField(name=f"observation.{obs.name}", value=obs.value) for obs in signal.evidence
    ]
    return EvidenceBlock(label=LABEL_WASTE_SIGNAL, fields=tuple(fields))


def _ranked_evidence(
    recommendation: Recommendation, score: RankingScore, total: int
) -> EvidenceBlock:
    """Render a finished rank for the model to narrate, not to revise."""
    return EvidenceBlock(
        label=LABEL_RANKED,
        fields=(
            EvidenceField(name="recommendation_id", value=recommendation.recommendation_id),
            EvidenceField(name="rank", value=str(recommendation.rank)),
            EvidenceField(name="rank_total", value=str(total)),
            EvidenceField(name="resource_name", value=recommendation.target_resource_name),
            EvidenceField(name="action", value=recommendation.action.value),
            EvidenceField(name="action_class", value=recommendation.risk_class.value),
            EvidenceField(name="confidence", value=str(recommendation.confidence)),
            EvidenceField(name="monthly_saving", value=recommendation.savings.monthly.display()),
            EvidenceField(name="score_breakdown", value=score.explain()),
        ),
    )


def _environments(estate: Estate) -> dict[str, Environment]:
    return {sub.subscription_id: sub.environment for sub in estate.subscriptions}


def _rationale_provenance(llm_name: str, signal_id: str) -> Provenance:
    """Record that the rationale -- and only the rationale -- is model-derived."""
    return Provenance(
        source=ProvenanceSource.LLM_INFERENCE,
        reference=f"{llm_name}:{TASK_PLANNING.name}:{signal_id}",
        verification=Verification.UNVERIFIED,
    )


def _resolve_action(
    *,
    signal: WasteSignal,
    item: PlannedRemediation | None,
    options: Sequence[PricedOption],
    errors: list[str],
) -> tuple[ActionType, str]:
    """Accept the model's action only if it was actually offered.

    Falls back to the policy's preferred action and records why, rather than
    aborting: one bad response should cost a rationale, not a whole sweep.
    """
    permitted = [option.action for option in options]
    fallback = permitted[0]
    fallback_rationale = (
        f"{signal.resource_name} was flagged as "
        f"{signal.kind.value.replace('_', ' ')} by {signal.detector}. The policy "
        f"store's preferred remediation for this waste kind is "
        f"{fallback.value.replace('_', ' ')}."
    )

    if item is None:
        errors.append(
            f"planner: the model returned no proposal for signal {signal.signal_id}; "
            f"fell back to the policy's preferred action {fallback.value}."
        )
        return fallback, fallback_rationale

    if item.action not in permitted:
        errors.append(
            f"planner: the model proposed action {item.action.value} for signal "
            f"{signal.signal_id}, which is not among the permitted candidates "
            f"({', '.join(a.value for a in permitted)}); rejected and fell back to "
            f"{fallback.value}."
        )
        return fallback, fallback_rationale

    return item.action, item.rationale


def make_optimization_planner(
    *,
    llm: LLM,
    settings: Settings,
    policy: PolicyStore | None = None,
) -> Node:
    """Build the Optimization Planner node."""
    _ = settings
    store = policy or PolicyStore()

    def optimization_planner(state: ScanState) -> NodeUpdate:
        """Choose, classify and deterministically rank a remediation per signal."""
        if state.estate is None or not state.signals:
            return {
                "audit": with_audit(
                    state,
                    [
                        audit(
                            state,
                            actor=ACTOR,
                            event_type=AuditEventType.RECOMMENDATION_PROPOSED,
                            subject=state.client,
                            detail={"recommendations": "0", "reason": "no signals to plan for"},
                        )
                    ],
                )
            }

        estate = state.estate
        environments = _environments(estate)
        options_by_signal = {
            signal.signal_id: (
                state.options_for(signal.signal_id)
                or tuple(
                    PricedOption(
                        signal_id=signal.signal_id,
                        action=action,
                        savings=SavingsEstimate.undetermined(
                            "no priced option was produced for this action"
                        ),
                    )
                    for action in store.candidate_actions(signal.kind)
                )
            )
            for signal in state.signals
        }

        plan = llm.structured(
            task=TASK_PLANNING,
            prompt=Prompt(
                instruction=_PLAN_INSTRUCTION,
                output_contract=_PLAN_CONTRACT,
                evidence=tuple(
                    _signal_evidence(
                        signal,
                        options_by_signal[signal.signal_id],
                        (
                            cause.narrative
                            if (cause := state.root_cause_for(signal.signal_id))
                            else None
                        ),
                    )
                    for signal in state.signals
                ),
            ),
            schema=RemediationPlan,
        )

        errors: list[str] = list(state.errors)
        proposed: list[Recommendation] = []

        for signal in state.signals:
            options = options_by_signal[signal.signal_id]
            action, rationale = _resolve_action(
                signal=signal,
                item=plan.for_signal(signal.signal_id),
                options=options,
                errors=errors,
            )
            chosen = next((o for o in options if o.action is action), None)
            savings = (
                chosen.savings
                if chosen is not None
                else SavingsEstimate.undetermined(
                    f"no priced option for {action.value} on {signal.resource_name}"
                )
            )
            proposed.append(
                Recommendation(
                    recommendation_id=f"rec-{signal.signal_id.removeprefix('ws-')}",
                    signal_id=signal.signal_id,
                    client=state.client,
                    subscription_id=signal.subscription_id,
                    target_resource_id=signal.resource_id,
                    target_resource_name=signal.resource_name,
                    action=action,
                    risk_class=store.classify(
                        action,
                        environment=environments.get(signal.subscription_id, Environment.UNKNOWN),
                    )[0],
                    rationale=rationale,
                    rationale_provenance=_rationale_provenance(llm.name, signal.signal_id),
                    preconditions=store.preconditions(action),
                    savings=savings,
                    confidence=signal.confidence,
                    # Placeholder: the real rank is assigned below, from the scores.
                    rank=1,
                )
            )

        # --- deterministic ranking ---------------------------------------
        scores = score_recommendations(proposed)
        order = rank_order(proposed, scores)
        rank_by_id = {rec_id: position for position, rec_id in enumerate(order, start=1)}
        ranked = tuple(
            sorted(
                (
                    rec.model_copy(update={"rank": rank_by_id[rec.recommendation_id]})
                    for rec in proposed
                ),
                key=lambda rec: rec.rank,
            )
        )
        score_by_id = {score.recommendation_id: score for score in scores}

        # --- model-authored narration of a finished ranking ---------------
        rationale = llm.structured(
            task=TASK_PLANNING,
            prompt=Prompt(
                instruction=_RANK_INSTRUCTION,
                output_contract=_RANK_CONTRACT,
                evidence=tuple(
                    _ranked_evidence(rec, score_by_id[rec.recommendation_id], len(ranked))
                    for rec in ranked
                ),
            ),
            schema=RankingRationale,
        )
        ranking = tuple(
            score_by_id[rec.recommendation_id].model_copy(
                update={
                    "rationale": rationale.note_for(rec.recommendation_id),
                    "rationale_provenance": _rationale_provenance(llm.name, rec.recommendation_id),
                }
            )
            for rec in ranked
        )

        events = [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.RECOMMENDATION_PROPOSED,
                subject=rec.recommendation_id,
                detail={
                    "signal_id": rec.signal_id,
                    "action": rec.action.value,
                    "risk_class": rec.risk_class.value,
                    "resource": rec.target_resource_name,
                    "monthly_saving": rec.savings.monthly.display(),
                    "rank": str(rec.rank),
                    "score": score_by_id[rec.recommendation_id].explain(),
                    "model": llm.name,
                },
                offset=offset,
            )
            for offset, rec in enumerate(ranked)
        ]

        _log.info(
            "remediations planned and ranked",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "signals": len(state.signals),
                "recommendations": len(ranked),
                "top_ranked": ranked[0].target_resource_name if ranked else None,
                "model": llm.name,
            },
        )

        return {
            "recommendations": ranked,
            "ranking": ranking,
            "errors": tuple(errors),
            "audit": with_audit(state, events),
        }

    return optimization_planner
