"""The policy store: the action-class floor, candidate actions and preconditions.

Three things live here, and all three are data rather than scattered logic.

**The floor.** :data:`ACTION_CLASS_FLOOR` gives every action its minimum class.
Client policy may *tighten* a class and may never loosen it below the floor
(ARCHITECTURE.md D8), which is enforced by
:meth:`~costsentinel.domain.recommendations.ActionClass.at_least` rather than by
reviewer vigilance. A misconfiguration cannot make deleting a resource automatic.

**Candidate actions.** :data:`CANDIDATE_ACTIONS` is the closed set of actions
permitted for each waste kind. The planner offers only these to the model, and
rejects a response naming anything else -- so the action vocabulary is constrained
twice: once by the enum, once by the per-kind candidate set.

**Preconditions.** :data:`PRECONDITIONS` are the checks re-run immediately before
execution. They come from here and never from a model response, so a compromised
response cannot drop the check that a disk is still unattached before it is deleted.
"""

from __future__ import annotations

from costsentinel.domain.common import Provenance, ProvenanceSource, Verification
from costsentinel.domain.estate import Environment
from costsentinel.domain.governance import ActionClassification
from costsentinel.domain.recommendations import ActionClass, ActionType
from costsentinel.domain.signals import WasteKind

#: The minimum class for every action. Nothing may be classified below this.
ACTION_CLASS_FLOOR: dict[ActionType, ActionClass] = {
    ActionType.NO_ACTION: ActionClass.ALLOW,
    ActionType.APPLY_TAG: ActionClass.ALLOW,
    ActionType.NOTIFY_OWNER: ActionClass.ALLOW,
    ActionType.SNAPSHOT_MANAGED_DISK: ActionClass.ALLOW,
    ActionType.DEALLOCATE_VIRTUAL_MACHINE: ActionClass.REVIEW,
    ActionType.RESIZE_VIRTUAL_MACHINE: ActionClass.REVIEW,
    ActionType.SCALE_DOWN_SQL_DATABASE: ActionClass.REVIEW,
    ActionType.SCALE_DOWN_APP_SERVICE_PLAN: ActionClass.REVIEW,
    # Exchanging a reservation is a commercial act against a contract, not just a
    # technical change, so it always needs a human even though nothing is deleted.
    ActionType.EXCHANGE_UNUSED_RESERVATION: ActionClass.REVIEW,
    ActionType.DELETE_STALE_SNAPSHOT: ActionClass.REVIEW,
    ActionType.DELETE_ORPHANED_MANAGED_DISK: ActionClass.REVIEW,
    ActionType.DELETE_UNATTACHED_PUBLIC_IP: ActionClass.REVIEW,
    # Deleting something not positively confirmed orphaned is never automated,
    # regardless of who approves it (CLAUDE.md section 7, ARCHITECTURE.md D9).
    ActionType.DELETE_UNCONFIRMED_RESOURCE: ActionClass.BLOCK,
}

#: Actions permitted for each waste kind, most-preferred first. The planner offers
#: these to the model as ``candidate_actions`` and will accept nothing else.
CANDIDATE_ACTIONS: dict[WasteKind, tuple[ActionType, ...]] = {
    WasteKind.ORPHANED_MANAGED_DISK: (
        ActionType.DELETE_ORPHANED_MANAGED_DISK,
        ActionType.SNAPSHOT_MANAGED_DISK,
        ActionType.NOTIFY_OWNER,
    ),
    WasteKind.UNATTACHED_PUBLIC_IP: (
        ActionType.DELETE_UNATTACHED_PUBLIC_IP,
        ActionType.NOTIFY_OWNER,
    ),
    WasteKind.IDLE_VIRTUAL_MACHINE: (
        ActionType.DEALLOCATE_VIRTUAL_MACHINE,
        ActionType.RESIZE_VIRTUAL_MACHINE,
        ActionType.NOTIFY_OWNER,
    ),
    WasteKind.OVERSIZED_VIRTUAL_MACHINE: (
        ActionType.RESIZE_VIRTUAL_MACHINE,
        ActionType.NOTIFY_OWNER,
    ),
    WasteKind.STALE_SNAPSHOT: (
        ActionType.DELETE_STALE_SNAPSHOT,
        ActionType.NOTIFY_OWNER,
    ),
    WasteKind.IDLE_SQL_DATABASE: (
        # Scaling down is the conservative remediation. Deleting or pausing an idle
        # database would save more, but "no connections for 30 days" is not proof
        # that nothing needs it, so the safe action is offered first.
        ActionType.SCALE_DOWN_SQL_DATABASE,
        ActionType.NOTIFY_OWNER,
    ),
    WasteKind.OVERSIZED_APP_SERVICE_PLAN: (
        ActionType.SCALE_DOWN_APP_SERVICE_PLAN,
        ActionType.NOTIFY_OWNER,
    ),
    WasteKind.UNUSED_RESERVATION: (
        ActionType.EXCHANGE_UNUSED_RESERVATION,
        ActionType.NOTIFY_OWNER,
    ),
    WasteKind.STALE_NON_PRODUCTION_RESOURCE: (
        ActionType.NOTIFY_OWNER,
        ActionType.APPLY_TAG,
    ),
    WasteKind.UNEXPECTED_EGRESS: (ActionType.NOTIFY_OWNER,),
}

#: Conditions re-checked immediately before execution. The estate may have changed
#: while a human was deciding, so these are evaluated then, not when proposed.
PRECONDITIONS: dict[ActionType, tuple[str, ...]] = {
    ActionType.NO_ACTION: (),
    ActionType.APPLY_TAG: ("Resource still exists.",),
    ActionType.NOTIFY_OWNER: ("An owner tag or contact is resolvable.",),
    ActionType.SNAPSHOT_MANAGED_DISK: (
        "Resource still exists.",
        "Sufficient snapshot quota is available in the target region.",
    ),
    ActionType.DEALLOCATE_VIRTUAL_MACHINE: (
        "Virtual machine still exists and is still running.",
        "CPU utilisation has remained below the idle threshold since detection.",
        "The virtual machine is not a member of an availability set or scale set "
        "whose capacity guarantee would be affected.",
        "No active maintenance or change freeze applies to the subscription.",
    ),
    ActionType.RESIZE_VIRTUAL_MACHINE: (
        "Virtual machine still exists and is still running.",
        "The target SKU is available in the resource's region and zone.",
        "Observed peak CPU still fits the target SKU with the configured headroom.",
        "A restart window has been agreed -- resizing restarts the virtual machine.",
        "No active maintenance or change freeze applies to the subscription.",
    ),
    ActionType.DELETE_ORPHANED_MANAGED_DISK: (
        "Disk is still unattached, with no attachment recorded since detection.",
        "A snapshot has been taken and its restore has been verified.",
        "Disk is not referenced by any image, restore point or backup policy.",
        "No active maintenance or change freeze applies to the subscription.",
    ),
    ActionType.DELETE_UNATTACHED_PUBLIC_IP: (
        "Address is still unassociated with any network interface or load balancer.",
        "Address does not appear in any DNS record, firewall allowlist or client "
        "configuration known to the client.",
        "No active maintenance or change freeze applies to the subscription.",
    ),
    ActionType.DELETE_UNCONFIRMED_RESOURCE: (
        "Not automatable: this action has no execution path in CostSentinel.",
    ),
    ActionType.DELETE_STALE_SNAPSHOT: (
        "Snapshot is still present and still past the retention threshold.",
        "Snapshot is not the most recent one for its source disk.",
        "Snapshot is not referenced by any image, restore point or backup policy.",
        "The source disk still exists, or its loss has been accepted in writing.",
        "No active maintenance or change freeze applies to the subscription.",
    ),
    ActionType.SCALE_DOWN_SQL_DATABASE: (
        "Database still exists and connection count has remained at zero since detection.",
        "No scheduled job, report or batch window depends on the current tier.",
        "The target tier supports the database's current size and feature usage.",
        "A brief failover has been agreed -- changing tier interrupts connections.",
        "No active maintenance or change freeze applies to the subscription.",
    ),
    ActionType.SCALE_DOWN_APP_SERVICE_PLAN: (
        "Plan still exists and utilisation has remained below the threshold since detection.",
        "No app on the plan requires a feature only the current tier provides.",
        "Observed peak still fits the target tier with the configured headroom.",
        "Autoscale rules have been reviewed against the smaller instance count.",
        "No active maintenance or change freeze applies to the subscription.",
    ),
    ActionType.EXCHANGE_UNUSED_RESERVATION: (
        "Reservation is still active and its utilisation is still below the threshold.",
        "The exchange or refund window permits the change under the current agreement.",
        "Finance has confirmed the commercial impact of the exchange.",
        "A replacement commitment has been sized against actual current usage.",
    ),
}

#: Environments where a destructive action is tightened beyond its floor.
#: Deleting anything in production is advice for a human, not something
#: CostSentinel will ever execute, however it is approved.
_PRODUCTION_LIKE: frozenset[Environment] = frozenset({Environment.PRODUCTION, Environment.STAGING})

_DESTRUCTIVE_DELETES: frozenset[ActionType] = frozenset(
    {
        ActionType.DELETE_ORPHANED_MANAGED_DISK,
        ActionType.DELETE_UNATTACHED_PUBLIC_IP,
        ActionType.DELETE_UNCONFIRMED_RESOURCE,
        ActionType.DELETE_STALE_SNAPSHOT,
    }
)


class PolicyStore:
    """Classifies actions, and answers what may be proposed and pre-checked.

    Phase 1 reads the built-in tables above. Phase 3 loads client overlays on top,
    which may only ever tighten a class.
    """

    @property
    def name(self) -> str:
        """Identifier recorded in classification provenance."""
        return "builtin-policy-v1"

    def floor_for(self, action: ActionType) -> ActionClass:
        """The built-in minimum class for an action.

        An action absent from the floor table is treated as :attr:`ActionClass.BLOCK`
        -- an unclassified action is never automatable.
        """
        return ACTION_CLASS_FLOOR.get(action, ActionClass.BLOCK)

    def candidate_actions(self, kind: WasteKind) -> tuple[ActionType, ...]:
        """Actions permitted for one waste kind, most-preferred first.

        A waste kind with no entry yields :attr:`ActionType.NOTIFY_OWNER`: tell a
        human, change nothing.
        """
        return CANDIDATE_ACTIONS.get(kind, (ActionType.NOTIFY_OWNER,))

    def preconditions(self, action: ActionType) -> tuple[str, ...]:
        """Conditions to re-check immediately before executing an action."""
        return PRECONDITIONS.get(action, ("Unrecognised action: no execution path.",))

    def classify(
        self,
        action: ActionType,
        *,
        environment: Environment = Environment.UNKNOWN,
    ) -> tuple[ActionClass, str]:
        """Classify an action in context, returning the class and the reason.

        Args:
            action: The proposed action.
            environment: The environment of the target resource's subscription.

        Returns:
            The effective class, never below the floor, and a plain-language reason.
        """
        floor = self.floor_for(action)
        effective = floor
        reason = f"Built-in floor for {action.value} is {floor.value}."

        if action in _DESTRUCTIVE_DELETES and environment in _PRODUCTION_LIKE:
            effective = effective.at_least(ActionClass.BLOCK)
            reason = (
                f"Built-in floor for {action.value} is {floor.value}; tightened to "
                f"{ActionClass.BLOCK.value} because the target is in a "
                f"{environment.value} environment, where CostSentinel never executes "
                f"a deletion."
            )
        elif action is ActionType.DEALLOCATE_VIRTUAL_MACHINE and environment in _PRODUCTION_LIKE:
            effective = effective.at_least(ActionClass.REVIEW)
            reason = (
                f"Built-in floor for {action.value} is {floor.value}; a "
                f"{environment.value} workload additionally requires an agreed "
                f"change window at approval time."
            )

        return effective, reason

    def classification_for(
        self,
        *,
        recommendation_id: str,
        action: ActionType,
        environment: Environment = Environment.UNKNOWN,
    ) -> ActionClassification:
        """Build the audit-grade classification record for one recommendation."""
        effective, reason = self.classify(action, environment=environment)
        return ActionClassification(
            recommendation_id=recommendation_id,
            action=action,
            action_class=effective,
            floor_class=self.floor_for(action),
            requires_approval=effective.requires_approval,
            is_automatable=effective.is_automatable,
            policy_reference=f"{self.name}:{action.value}:{environment.value}",
            reason=reason,
            provenance=Provenance(
                source=ProvenanceSource.POLICY_STORE,
                reference=f"{self.name}:ACTION_CLASS_FLOOR",
                verification=Verification.VERIFIED,
            ),
        )
