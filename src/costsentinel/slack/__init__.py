"""The Slack approval adapter (Phase 10b): a gated decision, reaching a human in chat.

Outbound, a pending approval is rendered as a Block Kit message from the report's own
strings (:mod:`.blocks`) and sent through a client seam (:mod:`.client`). Inbound, a
button click arrives at a webhook (:mod:`.webhook`). It is signature-verified on the
raw bytes (:mod:`.signature`), parsed into a typed interaction (:mod:`.interaction`),
and decided through the one decision path every surface uses.

Off by default. Nothing here can act on a resource: an approval from Slack hands the
action to a person, exactly as one from the CLI does (D135).
"""
