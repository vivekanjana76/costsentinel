"""MCP tool server (Phase 2).

A FastMCP server will expose Azure Cost Management, Resource Graph, Advisor and
Monitor as typed tools. The boundary earns its place by being the single location
for the mock/real swap, read-only enforcement, caching, retry and rate limiting --
applied once rather than per agent. The mock provider will mirror the identical tool
interface, so agents cannot tell the difference and tests exercise the real call
path. Agents never import an Azure SDK directly.

See ARCHITECTURE.md section 10 and ROADMAP.md Phase 2.
"""
