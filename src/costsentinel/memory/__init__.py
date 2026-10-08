"""Semantic and episodic memory (Phase 4).

Two stores on PostgreSQL with pgvector, deliberately separate because they answer
different questions (ARCHITECTURE.md D10):

* **Semantic** -- "what is true in general": Azure pricing, the Well-Architected
  cost pillar, the org FinOps policy, per-client contractual constraints. Retrieval
  is filtered by client so one client's policy cannot leak into another's reasoning.
* **Episodic** -- "what happened here before": past findings, decisions, and whether
  a change was accepted, rejected or reverted, with the stated reason. This is what
  lets the planner demote a recommendation a client has already rejected three
  times instead of re-proposing it cold.

Both are optional at runtime: with no database configured the system degrades to
stateless operation, logs that it is doing so, and keeps working (D11).

See ARCHITECTURE.md section 6 and ROADMAP.md Phase 4.
"""
