"""Root-Cause Analyst: explain why each signal exists, from its own evidence.

The node is LLM-assisted, and it is the clearest illustration of what "the model
explains, it does not assert" means in practice. The Anomaly Scout has already
established *that* a resource is wasteful and *what it costs*. This node adds only
the narrative, and it is held to the evidence in two ways:

* the structured contract has no numeric fields, so no quantity can enter here;
* every contributing factor must name an observation the detector actually attached
  to the signal, and a factor citing anything else is dropped with the rejection
  recorded in ``state.errors``.

That second check is what makes groundedness a property rather than a hope. A model
that invents "the owner left the company" cannot get it into a report, because
there is no observation by that name to cite.
"""

from __future__ import annotations

from costsentinel.agents.base import Node, NodeUpdate, audit, with_audit
from costsentinel.config import Settings
from costsentinel.domain.analysis import ContributingFactor, RootCause
from costsentinel.domain.common import Provenance, ProvenanceSource, Verification
from costsentinel.domain.governance import AuditEventType
from costsentinel.domain.signals import WasteSignal
from costsentinel.domain.state import ScanState
from costsentinel.llm.base import LLM, EvidenceBlock, EvidenceField, Prompt
from costsentinel.llm.contracts import RootCauseAnalysis, RootCauseExplanation
from costsentinel.llm.fake import LABEL_WASTE_SIGNAL
from costsentinel.llm.routing import TASK_ROOT_CAUSE
from costsentinel.observability.logging import get_logger

ACTOR = "root_cause_analyst"

_log = get_logger("agents.root_cause_analyst")

_INSTRUCTION = """
You are the Root-Cause Analyst for a FinOps governance system operating Azure
subscriptions on behalf of enterprise and government clients.

For each waste_signal block below, explain why that waste exists. Correlate the
resource's configuration, its measured utilisation and its age.

Rules you must follow:
- Every contributing factor must cite one of the observation.* field names given for
  that signal, in its evidence_name. Do not cite anything else.
- Do not introduce a fact, a resource, an owner or an event that is not in the
  evidence. If the evidence does not explain something, say that it does not.
- Do not state, estimate or infer any monetary amount. Costs are computed elsewhere.
  If you mention a cost, copy it verbatim from the evidence.
- Explain the cause, not the remedy. Remediation is another agent's job.
""".strip()

_OUTPUT_CONTRACT = (
    "RootCauseAnalysis: explanations[] of {signal_id, narrative, "
    "factors[] of {kind, statement, evidence_name}}. One explanation per "
    "waste_signal block. No numeric fields."
)


def _signal_evidence(signal: WasteSignal) -> EvidenceBlock:
    """Render one signal as evidence for diagnosis.

    Carries the same ``observation.*`` naming the planner uses, so the names a model
    is allowed to cite are exactly the names it was shown.
    """
    fields = [
        EvidenceField(name="signal_id", value=signal.signal_id),
        EvidenceField(name="waste_kind", value=signal.kind.value),
        EvidenceField(name="resource_name", value=signal.resource_name),
        EvidenceField(name="resource_kind", value=signal.resource_kind),
        EvidenceField(name="subscription_id", value=signal.subscription_id),
        EvidenceField(name="detector", value=signal.detector),
        EvidenceField(name="detected_at", value=signal.detected_at.isoformat()),
        EvidenceField(name="current_monthly_cost", value=signal.monthly_cost.display()),
    ]
    fields += [
        EvidenceField(name=f"observation.{obs.name}", value=obs.value) for obs in signal.evidence
    ]
    return EvidenceBlock(label=LABEL_WASTE_SIGNAL, fields=tuple(fields))


def _narrative_provenance(llm_name: str, signal_id: str) -> Provenance:
    """Record that the narrative, and only the narrative, is model-derived."""
    return Provenance(
        source=ProvenanceSource.LLM_INFERENCE,
        reference=f"{llm_name}:{TASK_ROOT_CAUSE.name}:{signal_id}",
        verification=Verification.UNVERIFIED,
    )


def _grounded_factors(
    *,
    signal: WasteSignal,
    explanation: RootCauseExplanation,
    errors: list[str],
) -> tuple[ContributingFactor, ...]:
    """Keep only the factors that cite evidence the signal actually carries."""
    available = {obs.name for obs in signal.evidence}
    kept: list[ContributingFactor] = []
    for factor in explanation.factors:
        if factor.evidence_name not in available:
            errors.append(
                f"root_cause_analyst: dropped a factor for signal {signal.signal_id} "
                f"citing evidence {factor.evidence_name!r}, which the detector did not "
                f"record. Available: {', '.join(sorted(available)) or 'none'}."
            )
            continue
        kept.append(
            ContributingFactor(
                kind=factor.kind,
                statement=factor.statement,
                evidence_name=factor.evidence_name,
            )
        )
    return tuple(kept)


def _fallback_narrative(signal: WasteSignal) -> str:
    """A grounded explanation built without a model, for when one is unavailable."""
    observations = "; ".join(f"{obs.name} is {obs.value}" for obs in signal.evidence)
    return (
        f"{signal.resource_name} was flagged as "
        f"{signal.kind.value.replace('_', ' ')} by {signal.detector}. The detector's "
        f"evidence: {observations or 'no observations were recorded'}. No narrative "
        f"explanation was available for this signal, so this is the detector's own "
        f"finding restated."
    )


def make_root_cause_analyst(*, llm: LLM, settings: Settings) -> Node:
    """Build the Root-Cause Analyst node."""
    _ = settings

    def root_cause_analyst(state: ScanState) -> NodeUpdate:
        """Diagnose every signal, keeping only evidence-grounded factors."""
        if not state.signals:
            return {
                "audit": with_audit(
                    state,
                    [
                        audit(
                            state,
                            actor=ACTOR,
                            event_type=AuditEventType.SIGNAL_DETECTED,
                            subject=state.client,
                            detail={"root_causes": "0", "reason": "no signals to diagnose"},
                        )
                    ],
                )
            }

        prompt = Prompt(
            instruction=_INSTRUCTION,
            output_contract=_OUTPUT_CONTRACT,
            evidence=tuple(_signal_evidence(signal) for signal in state.signals),
        )
        analysis = llm.structured(task=TASK_ROOT_CAUSE, prompt=prompt, schema=RootCauseAnalysis)

        errors: list[str] = list(state.errors)
        root_causes: list[RootCause] = []

        for signal in state.signals:
            explanation = analysis.for_signal(signal.signal_id)
            if explanation is None:
                errors.append(
                    f"root_cause_analyst: the model returned no explanation for signal "
                    f"{signal.signal_id}; fell back to restating the detector's evidence."
                )
                root_causes.append(
                    RootCause(
                        signal_id=signal.signal_id,
                        narrative=_fallback_narrative(signal),
                        narrative_provenance=Provenance.calculated(
                            f"detector evidence for {signal.signal_id}, restated"
                        ),
                        factors=(),
                        # The detector's own confidence is unaffected by the model
                        # failing to narrate it.
                        confidence=signal.confidence,
                        analysed_at=signal.detected_at,
                    )
                )
                continue

            factors = _grounded_factors(signal=signal, explanation=explanation, errors=errors)
            root_causes.append(
                RootCause(
                    signal_id=signal.signal_id,
                    narrative=explanation.narrative,
                    narrative_provenance=_narrative_provenance(llm.name, signal.signal_id),
                    factors=factors,
                    confidence=signal.confidence,
                    analysed_at=signal.detected_at,
                )
            )

        events = [
            audit(
                state,
                actor=ACTOR,
                event_type=AuditEventType.SIGNAL_DETECTED,
                subject=cause.signal_id,
                detail={
                    "root_cause": "diagnosed",
                    "factors": str(len(cause.factors)),
                    "cited_evidence": ",".join(cause.cited_evidence),
                    "model": llm.name,
                },
                offset=offset,
            )
            for offset, cause in enumerate(root_causes)
        ]

        _log.info(
            "root causes analysed",
            extra={
                "run_id": state.run_id,
                "client": state.client,
                "signals": len(state.signals),
                "root_causes": len(root_causes),
                "dropped_factors": len(errors) - len(state.errors),
                "model": llm.name,
            },
        )

        return {
            "root_causes": tuple(root_causes),
            "errors": tuple(errors),
            "audit": with_audit(state, events),
        }

    return root_cause_analyst
