# SOURCE_MAP — Claude Codex

| Source | This plugin | Taken |
| --- | --- | --- |
| hermes-agent anthropic transport | `chat.py` | Messages system/messages split, tools input_schema |
| hermes-agent Claude Code creds | `subscription_auth.py` | Keychain + ~/.claude/.credentials.json, refresh endpoints |
| Meapri/hermes-claude-auth | `subscription_fingerprint.py` | Billing header, system identity, stainless headers, plan-lane shaping |
| — | omitted | Hermes AIAgent loop, gateway, sitecustomize monkey-patch installer |
