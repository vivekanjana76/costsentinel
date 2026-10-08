"""Report rendering (Phase 6).

Markdown and PDF rendering of a
:class:`~costsentinel.domain.report.ClientReport`, with per-client branding and a
realised-versus-projected savings section driven by episodic memory.

The hard requirement on this package is a test asserting that every number in a
rendered report traces to a state field -- rendering must not become a place where
an ungrounded figure can appear. Phase 1 serialises the report as JSON instead; the
CLI's ``--summary`` is a terminal digest, not a report.

See ROADMAP.md Phase 6.
"""
