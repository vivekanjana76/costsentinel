# Synthetic Slack payloads

Hand-written in the shape of Slack's documented `block_actions` interaction payload.
**Not recorded from a real workspace.** Every id here is invented and labelled: team
`T0SYNTH01`, users `U0ALICE01` / `U0MALLORY1` / `U0BOB0001`, user group `S0APPROVE1`,
channel `C0SYNTH01`. The `response_url` points at `hooks.slack.com` with a path that
was never issued. Tests sign these bodies with a labelled fake secret.

The tests fill in `{recommendation_id}`, `{action_id}`, `{action_ts}`, `{reason}` and
`{user_id}` before signing.
