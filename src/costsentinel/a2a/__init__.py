"""Agent2Agent (A2A) layer.

One read-only Agent Card in the A2A v1 wire shape, signed with a JWS per spec section
8.4, and a task lifecycle (submit, status, artefact, cancel) for three read-only skills
(Phase 10, completed in Phase 12). Per-specialist cards, an official protocol binding,
per-caller authorisation and consuming other agents are Phase 12b.

The governance requirement is the point: nothing on this surface can record a decision
or change a resource, so an external caller cannot route around the approval gate. MCP
is for tools we consume; A2A is for agents we expose (ARCHITECTURE.md D16).

See ARCHITECTURE.md section 13 and ROADMAP.md Phases 12 and 12b.
"""
