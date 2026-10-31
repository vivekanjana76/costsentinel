# The Agent Card, as served

`agent-card.json` is exactly what `GET /.well-known/agent-card.json` returns on the mock
estate. `agent-card-jwks.json` is the public key its `jku` names. They are committed so
a reviewer can check the card's JWS without running anything.

```bash
uv run python -c "import json, jwt; from costsentinel.a2a.card import canonical_payload; \
from costsentinel.a2a.signing import b64url; \
c=json.load(open('docs/a2a/agent-card.json')); s=c['signatures'][0]; \
k=jwt.PyJWK(json.load(open('docs/a2a/agent-card-jwks.json'))['keys'][0]); \
print(jwt.api_jws.decode_complete(s['protected']+'.'+b64url(canonical_payload(c))+'.'+s['signature'], key=k, algorithms=['ES256'])['header'])"
```

The signing key is a **synthetic** key, derived from a seed committed in
`src/costsentinel/a2a/signing.py` and labelled worthless. This signature therefore proves
the file has not been edited since it was generated. It proves nothing about who
generated it. A real deployment signs with its own key (`A2A_JWS_KEY_FILE`).

A test (`test_the_committed_card_is_the_served_card_and_still_verifies`) fails if the
file stops verifying, or stops matching what the server serves. To regenerate it after
a deliberate change to the card, see that test's docstring.
