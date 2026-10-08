"""Agent2Agent (A2A) layer (Phase 8).

A2A v1 Agent Cards publish each specialist as a discoverable, invocable agent, so an
Azure AI Foundry orchestrator or a client's own agent can call, say, the Savings
Estimator without adopting our graph.

The governance requirement is the point: a published card declares the action class
of anything its agent can propose, so an external caller inherits the same approval
gate and cannot route around it. MCP is for tools we consume; A2A is for agents we
expose (ARCHITECTURE.md D16).

See ARCHITECTURE.md section 10 and ROADMAP.md Phase 8.
"""
