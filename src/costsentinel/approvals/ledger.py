"""The decision ledger: what each surface asked for, and what came of it.

The checkpoint already holds every *decision*, written by the approval gate with its
audit events. Two things never reach the gate, and both still have to be on the record:

- **A refusal.** An unauthorised approver's click must be recorded as refused, never as
  a decision. It cannot be written to the checkpoint, because writing there means
  resuming the sweep, and resuming the sweep is exactly what a refusal does not do.
- **An interaction already answered.** Slack redelivers a callback when it does not see
  a fast enough response. The ledger keys each outcome by the interaction that asked
  for it, so a redelivery returns the stored outcome instead of deciding twice.

One SQLite file in the state directory, from the standard library, next to the
checkpoint and the memory store. Append-only in practice: the service inserts and never
updates. An ``interaction_key`` is unique when present. The dashboard and the CLI carry
no natural key, so for them the key is empty and the row is a plain log entry.

A request that fails signature verification never reaches this module, and writes
nothing anywhere (D134).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from costsentinel.approvals.identity import Surface
from costsentinel.domain.common import Frozen
from costsentinel.domain.governance import ApprovalRecord, AuditEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interaction_key TEXT UNIQUE,
    surface TEXT NOT NULL,
    kind TEXT NOT NULL,
    recommendation_id TEXT NOT NULL,
    outcome_json TEXT NOT NULL
)
"""


class OutcomeKind(StrEnum):
    """What one request to decide came to. Three members, and none of them executes."""

    #: The gate recorded a new decision.
    DECIDED = "decided"

    #: The item had already been resolved. The existing decision is returned unchanged.
    ALREADY_DECIDED = "already_decided"

    #: The approver may not decide this item. Recorded here, and the sweep is untouched.
    REFUSED = "refused"


class DecisionOutcome(Frozen):
    """The answer a surface gets back, and the row the ledger keeps.

    Carries the gate's record when there is one, copied as the gate wrote it, and the
    ledger's own audit event. It holds nothing that could act on the decision: no
    callable and no client, asserted against the field set like every approval type.
    """

    kind: OutcomeKind
    surface: Surface
    recommendation_id: str
    client: str
    run_id: str
    statement: str = Field(min_length=1, description="What happened, in plain words.")
    record: ApprovalRecord | None = Field(
        default=None, description="The gate's record: new when decided, existing when not."
    )
    remaining: int | None = Field(
        default=None, description="How many items the sweep still waits on, when it was resumed."
    )
    event: AuditEvent
    replayed: bool = Field(
        default=False,
        description="True when this was returned from the ledger for a repeated interaction.",
    )


class DecisionLedger:
    """A SQLite file of decision outcomes, keyed by interaction where there is one."""

    def __init__(self, path: Path) -> None:
        """Open, creating the file and its table if this is the first outcome."""
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(_SCHEMA)

    @property
    def path(self) -> Path:
        """Where the ledger lives."""
        return self._path

    def get(self, interaction_key: str) -> DecisionOutcome | None:
        """The stored outcome for one interaction, if it has been answered."""
        with closing(sqlite3.connect(self._path)) as db:
            row = db.execute(
                "SELECT outcome_json FROM outcomes WHERE interaction_key = ?", (interaction_key,)
            ).fetchone()
        if row is None:
            return None
        return DecisionOutcome.model_validate_json(str(row[0]))

    def append(self, outcome: DecisionOutcome, *, interaction_key: str | None = None) -> None:
        """Record one outcome.

        Raises:
            sqlite3.IntegrityError: If the interaction already has an outcome. The
                service checks first and holds a lock, so reaching this means a second
                process answered the same interaction; failing is the right response.
        """
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute(
                "INSERT INTO outcomes (interaction_key, surface, kind, recommendation_id, "
                "outcome_json) VALUES (?, ?, ?, ?, ?)",
                (
                    interaction_key,
                    outcome.surface.value,
                    outcome.kind.value,
                    outcome.recommendation_id,
                    outcome.model_dump_json(),
                ),
            )

    def all(self) -> tuple[DecisionOutcome, ...]:
        """Every outcome, oldest first. For the audit trail and for tests."""
        with closing(sqlite3.connect(self._path)) as db:
            rows = db.execute("SELECT outcome_json FROM outcomes ORDER BY id").fetchall()
        return tuple(DecisionOutcome.model_validate_json(str(row[0])) for row in rows)
