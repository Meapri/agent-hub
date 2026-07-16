---
name: google-antigravity
description: "Use Google Antigravity for consent-gated direct Google OAuth login, official agy sessions, chat (multimodal/tool-calls/streaming), writing, models, diagnostics, and capability-gated grounding or images."
---

# Google Antigravity

Use the `google-antigravity-codex` MCP tools for Antigravity workflows.

## Consent

Check `google_antigravity_consent_status` before an authenticated integration.
If consent is absent, tell the user to run this themselves:

```bash
python3 scripts/google_antigravity_consent.py grant --i-understand-and-consent
```

Never grant or modify consent through MCP.

## Direct Google login (preferred for grounding / image)

Same PKCE browser flow as Hermes `hermes auth add google-antigravity`.

1. `google_antigravity_login_status` — is a direct OAuth token present?
2. If not ready: `google_antigravity_login_start` → show the user `auth_url`.
3. User signs in with Google and pastes the redirect URL or `code=`.
4. `google_antigravity_login_complete` with `{ "code_or_url": "..." }`  
   - default `probe: true` verifies tokens via `list_models` (or a tiny chat).
5. After success, provider auto-selects **`agy-oauth`** (Code Assist HTTP).

CLI equivalent:

```bash
python3 scripts/google_antigravity_login.py interactive
```

Never print tokens, client secrets, or raw pending OAuth state.

## Auth status

- `google_antigravity_login_status` / `whoami` / `logout`
- `google_antigravity_agy_auth_status` / `_refresh` — plugin OAuth token file only

## Model selection

- `google_antigravity_list_models` — live/static catalog.
- `google_antigravity_get_model_prefs` / `set_model` / `clear_model_prefs` —
  persist default or per-task model (`chat`, `code`, `writing`, …).
- `google_antigravity_route_model` — task routing; respects saved prefs.
- After set, omit `model` on chat/write/search/image to use the preference.

## Model tools

- `google_antigravity_provider_status` before uncertain model calls.
- `google_antigravity_chat` — text, multimodal **data:** images, tool-calls, optional `stream: true`.
- `google_grounded_search` / `google_antigravity_generate_image` — require **`agy-oauth`**.
- `google_antigravity_write` — prose; **readme/technical-doc** are durable
  (fact pack, no git diary). Multi-step README → orchestrate-codex `durable_readme`.
- release snapshot/draft, list/route models, quota.

### Chat notes

- `messages` may include OpenAI-style `image_url` with `data:image/...;base64,...` (remote URLs are not fetched).
- Assistant `tool_calls` and `role: tool` function results map to Gemini functionCall/functionResponse.
- `stream: true` emits MCP `notifications/message` deltas then a final tool result (agy-oauth only; falls back if stream fails).

## Safety

Never print tokens, authorization headers, cookies, or raw credential files.
Do not read the official CLI keyring. Treat warnings as operational signals.
