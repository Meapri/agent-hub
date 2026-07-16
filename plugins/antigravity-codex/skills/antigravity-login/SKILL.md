---
name: antigravity-login
description: "Authenticate Google Antigravity for Codex: consent, direct OAuth login, whoami, logout, provider preference."
---

# Antigravity Login / Account

## Consent first

```bash
python3 scripts/google_antigravity_consent.py grant --i-understand-and-consent
```

Or check: `google_antigravity_consent_status`.

## Direct Google login (PKCE)

1. `google_antigravity_login_status`
2. `google_antigravity_login_start` → open `auth_url` for the user
3. User pastes redirect URL / code
4. `google_antigravity_login_complete` (`probe` defaults true)
5. `google_antigravity_whoami` — email + project (no secrets)

CLI: `python3 scripts/google_antigravity_login.py interactive`

## Logout

`google_antigravity_logout` removes local plugin tokens only.  
Optional `{ "forget_client": true }` also drops oauth-client.json.  
Does **not** touch macOS Keychain or revoke at Google.

## Provider preference

- `google_antigravity_set_provider` `{ "provider": "agy-oauth" }` or `"plugin-OAuth"`
- `google_antigravity_get_session_prefs`
- Env `GOOGLE_ANTIGRAVITY_PROVIDER` still wins over saved preference.

## Rules

Never print access tokens, refresh tokens, or client secrets.
Never scrape Keychain.
