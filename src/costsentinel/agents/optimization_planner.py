"""Optimization Planner: turn signals into ranked, costed, classified proposals.

The division of labour here is the heart of CLAUDE.md golden rule 5:

* the **model** chooses an action from the candidate set the policy store permits,
  and writes the rationale -- it explains and ranks;
* the **arithmetic** in :mod:`costsentinel.agents.savings` produces every figure;
* the **policy store** supplies the risk class and the preconditions.

The model's choice is re-validated against the candidate set after the response
comes back. The closed enum already stops an invented action from validating; this
second check stops a *valid but not offered* action -- resizing a disk, say -- from
slipping through. On rejection the node falls back to the policy's preferred action
and records the rejection in ``state.errors`` rather than failing the sweep.

Phase 3 splits the savings arithmetic out into its own Savings Estimator node and
inserts the Root-Cause Analyst ahead of this one.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.agents.savings import estimate_savings
from costsentinel.config import Settings
from costsentinel.domain.common import Provenance, ProvenanceSource, Verification
from costsentinel.domain.estate import Environment, Estate, SkuPrice
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.recommendations import ActionType, Recommendation, SavingsEstimate
from costsentinel.domain.signals import WasteSignal
from costsentinel.domain.state import ScanState
from costsentinel.guardrails.policy import PolicyStore
from costsentinel.llm.base import LLM, EvidenceBlock, EvidenceField, Prompt
from costsentinel.llm.contracts import PlannedRemediation, RemediationPlan
from costsentinel.llm.fake import LABEL_WASTE_SIGNAL
from costsentinel.llm.routing import TASK_PLANNING
from costsentinel.observability.logging import get_logger
from costsentinel.providers.base import AzureProvider

ACTOR = "optimization_planner"

_log = get_logger("agents.optimization_planner")

_INSTRUCTION = """
You are the Optimization Planner for a FinOps governance system operating Azure
subscriptions on behalf of enterprise and government clients.

For each waste_signal block below, choose exactly one action from that block's
candidate_actions and write a short rationale a client's finance lead could read.

Rules you must follow:
- Choose only from candidate_actions for that signal. Nothing else is permitted.
- Do not state, estimate or infer any monetary amount. Savings are computed
  separately from provider data. If you mention a cost, copy it verbatim from the
  evidence.
- Do not state preconditions. They come from the policy store.
- Rank by the cost at stake, highest first.
""".strip()

_OUTPUT_CONTRACT = (
    "RemediationPlan: items[] of {signal_id, action, rationale, priority}. "
    "One item per waste_signal block."
)

#: Rank assigned to a finding whose saving could not be priced: last, but still
#: reported. An unpriced finding is not a worthless one.
_UNPRICED_SORT_KEY = Decimal("-1")


def _signal_evidence(
    signal: WasteSignal,
    candidates: Sequence[ActionType],
) -> EvidenceBlock:
    """Render one signal as evidence, with its permitted actions.

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
            value=",".join(action.value for action in candidates),
        ),
    ]
    fields += [
        EvidenceField(name=f"observation.{obs.name}", value=obs.value) for obs in signal.evidence
    ]
    return EvidenceBlock(label=LABEL_WASTE_SIGNAL, fields=tuple(fields))


def _environments(estate: Estate) -> dict[str, Environment]:
    return {sub.subscription_id: sub.environment for sub in estate.subscriptions}


def _rationale_provenance(llm_name: str, signal_id: str) -> Provenance:
    """Record that the rationale -- and only the rationale -- is model-derived."""
    return Provenance(
        source=ProvenanceSource.LLM_INFERENCE,
        reference=f"{llm_name}:{TASK_PLANNING.name}:{signal_id}",
        verification=Verification.UNVERIFIED,
    )


def _sort_key(savings: SavingsEstimate) -> Decimal:
    return savings.monthly.amount if savings.monthly.amount is not None else _UNPRICED_SORT_KEY


def make_optimization_planner(
    *,
    provider: AzureProvider,
    llm: LLM,
    settings: Settings,
    policy: PolicyStore | None = None,
) -> Node:
    """Build the Optimization Planner node."""
    store = policy or PolicyStore()

    def optimization_planner(state: ScanState) -> NodeUpdate:
        """Propose, cost, classify and rank a remediation for each signal."""
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
        candidates_by_signal = {
            signal.signal_id: store.candidate_actions(signal.kind) for signal in state.signals
        }

        prompt = Prompt(
            instruction=_INSTRUCTION,
            output_contract=_OUTPUT_CONTRACT,
            evidence=tuple(
                _signal_evidence(signal, candidates_by_signal[signal.signal_id])
                for signal in state.signals
            ),
        )
        plan = llm.structured(task=TASK_PLANNING, prompt=prompt, schema=RemediationPlan)

        price_cache: dict[str, Sequence[SkuPrice]] = {}
        errors: list[str] = list(state.errors)
        drafts: list[tuple[WasteSignal, ActionType, str, SavingsEstimate]] = []

        for signal in state.signals:
            candidates = candidates_by_signal[signal.signal_id]
            item = plan.for_signal(signal.signal_id)
            action, rationale = _resolve_action(
                signal=signal, item=item, candidates=candidates, errors=errors
            )

            resource = estate.resource(signal.resource_id)
            if resource is None:  # pragma: no cover -- signals are built from the estate
                errors.append(
                    f"planner: signal {signal.signal_id} targets unknown resource "
                    f"{signal.resource_id}; skipped."
                )
                continue

            if resource.region not in price_cache:
                price_cache[resource.region] = provider.list_sku_prices(resource.region)

            savings = estimate_savings(
                action,
                resource,
                estate.metrics_for(resource.resource_id),
                price_cache[resource.region],
                headroom_factor=settings.rightsize_headroom_factor,
            )
            drafts.append((signal, action, rationale, savings))

        drafts.sort(key=lambda d: (-_sort_key(d[3]), d[0].signal_id))

        recommendations = tuple(
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
                rank=rank,
            )
            for rank, (signal, action, rationale, savings) in enumerate(drafts, start=1)
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
                    "savings_is_estimated": str(rec.savings.is_estimated).lower(),
                    "rank": str(rec.rank),
                    "model": llm.name,
                },
                offset=offset,
            )
            for offset, rec in enumerate(recommendations)
        ]

        _log.info(
            "remediations planned",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "signals": len(state.signals),
                "recommendations": len(recommendations),
                "model": llm.name,
            },
        )

        return {
            "recommendations": recommendations,
            "errors": tuple(errors),
            "audit": with_audit(state, events),
        }

    return optimization_planner


def _resolve_action(
    *,
    signal: WasteSignal,
    item: PlannedRemediation | None,
    candidates: Sequence[ActionType],
    errors: list[str],
) -> tuple[ActionType, str]:
    """Accept the model's action only if it was actually offered.

    Falls back to the policy's preferred action and records why, rather than
    aborting: one bad response should cost a rationale, not a whole sweep.
    """
    fallback = candidates[0]
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

    if item.action not in candidates:
        errors.append(
            f"planner: the model proposed action {item.action.value} for signal "
            f"{signal.signal_id}, which is not among the permitted candidates "
            f"({', '.join(a.value for a in candidates)}); rejected and fell back to "
            f"{fallback.value}."
        )
        return fallback, fallback_rationale

    return item.action, item.rationale
