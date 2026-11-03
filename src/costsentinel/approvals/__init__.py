"""Deciding a gated recommendation: identity, authorisation, and the one decision path.

Every surface that can decide -- the CLI, the dashboard and the Slack adapter --
establishes who is deciding in its own way and then goes through this package, so
"who may approve" and "what a decision does" each have one answer (ARCHITECTURE.md
D132, D133).
"""
