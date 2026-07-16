---
name: antigravity-doctor
description: "Verify and troubleshoot the Google Antigravity plugin: consent, direct OAuth login, agy providers, MCP, models, diagnostics, grounding, writing, releases, and images."
---

# Antigravity Doctor

1. Check `google_antigravity_consent_status`; report the state without changing it.
2. Check `google_antigravity_cli_status`, plugin version, MCP `initialize`, and
   `tools/list`.
3. Check **direct login**: `google_antigravity_login_status`.
   - Not ready → guide the user through `login_start` → browser → `login_complete`
     (or `python3 scripts/google_antigravity_login.py interactive`).
4. For model workflows, check `google_antigravity_provider_status` and then the
   model list or quota-availability result.
5. When `agy-oauth` is selected (or direct login succeeded), check
   `google_antigravity_agy_auth_status` as a secondary export diagnostic.
6. Run only the focused smoke checks requested by the user.
7. Do not treat `agy models` success alone as proof that Code Assist HTTP works;
   after login, `login_complete` probe (or list_models) is stronger.

CLI:

```bash
python3 scripts/google_antigravity_doctor.py --json
python3 scripts/google_antigravity_doctor.py --json --live
```

Never read or copy the official CLI keyring. Never print tokens, secrets,
cookies, or authorization headers. If consent is absent, give the local grant
command and let the user run it.
