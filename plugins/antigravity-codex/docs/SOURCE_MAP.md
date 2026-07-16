# Source map: Hermes → Antigravity Codex

This plugin re-expresses capabilities from two upstream trees as a **Codex /
agy** package (skills + MCP), not as a Hermes core patch.

## Upstream inputs

| Source | What we take | What we leave behind |
| --- | --- | --- |
| [Meapri/hermes-google-antigravity-plugin](https://github.com/Meapri/hermes-google-antigravity-plugin) | Cloud Code PA endpoint (`cloudcode-pa.googleapis.com/v1internal`), generateContent request shape, model aliases, capacity fallback chain, image/grounding tool ideas, Hermes OAuth file field names | Hermes `agent/*.py` modules, core patches, `hermes auth` / provider registry edits, vendored proxy runtime, OAuth client ID/secret handling |
| [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent) | Provider-plugin packaging shape (`plugin.yaml` / `ProviderProfile` lessons), `openai-codex` as the OAuth-external peer pattern, skill layout ideas, security posture for external tools | Hermes runtime, transports, credential pool, gateway, CLI auth flows |

## Capability mapping

| Hermes / Antigravity concept | This plugin |
| --- | --- |
| `plugins/model-providers/google-antigravity` + native chat shim | MCP `google_antigravity_chat` + provider selection (`agy-cli` / `agy-oauth`) |
| Cloud Code PA `generateContent` | `google_antigravity_codex/antigravity_api.py` |
| `run_antigravity_login()` PKCE + localhost:51121 | `oauth_login.py` + MCP login_start/complete + `scripts/google_antigravity_login.py` |
| OAuth file (`access`/`refresh`/`expires`) | `agy_auth.py` + plugin `oauth-token.json` / Hermes path / agy export |
| `plugins/image_gen/google-antigravity` | MCP `google_antigravity_generate_image` (requires `agy-oauth`) |
| `plugins/web/google_grounding` | MCP `google_grounded_search` (requires `agy-oauth`) |
| `hermes model` / model catalog | `google_antigravity_list_models` + `google_antigravity_route_model` + skills |
| Hermes core patch (runtime_provider, auth_commands, …) | **Not ported** — Codex cannot swap the host model provider the same way |

## Auth boundary (important)

Hermes’ plugin obtains Google OAuth via PKCE and stores tokens under
`~/.hermes/auth/`. This Codex plugin **does not** embed Antigravity OAuth client
credentials or scrape Keychain.

Supported transports:

1. **`agy-cli` (default)** — official `agy` subprocess owns the session.
2. **`agy-oauth` (explicit)** — read a user-provided JSON token export
   (`GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE`, default
   `~/.gemini/antigravity-cli/antigravity-oauth-token`, or existing Hermes
   `~/.hermes/auth/google_antigravity.json`).

All model-facing tools require recorded consent
(`scripts/google_antigravity_consent.py` or process env).

## Layout

```text
.codex-plugin/plugin.json   # Codex plugin manifest
.mcp.json                   # Codex-hosted MCP server entry
plugin.json / mcp_config.json  # Native agy bundle (recursive tools disabled)
google_antigravity_codex/   # Python package (MCP tools + providers)
skills/                     # Agent skills for Codex / agy
scripts/                    # consent, doctor, MCP entry, release helpers
tests/                      # unit + contract tests
```
