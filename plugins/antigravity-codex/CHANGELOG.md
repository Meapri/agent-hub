# Changelog

All notable changes are documented in this file.

## 0.9.8 - 2026-07-16

### Changed (auth)

- **Removed agy-cli model transport** and any pull of official `agy` CLI/Keychain
  sessions for chat/grounding/image.
- Auth is **plugin Google OAuth only** (`login_start` / `login_complete` /
  `scripts/google_antigravity_login.py` → `oauth-token.json`).
- Token refresh uses Google token endpoint only (no `agy --prompt` poke).
- MCP no longer exposes `cli_status` / `cli_chat` tools.

## 0.9.7 - 2026-07-16

### Stability / quality

- Provider resolution: clear priority env → saved pref → auto (no double-env reads).
- Profile chat defaults: never override explicit caller values (including
  `grounding: "off"`).
- OAuth pending state no longer stores `client_secret` on disk; secrets
  re-resolved at complete time.
- Atomic secure JSON writes (`io_util.write_json_secure`) for tokens/prefs.
- Unified token path candidates for login/read/logout.
- Public `resolve_project_id`; cleaner diff-review repo resolution.
- Hardening tests for provider priority, profiles, pending security, paths.

## 0.9.6 - 2026-07-16

### Added

- **Session profiles**: balanced / coding / writing / research / fast / pair + custom
  (`list_profiles`, `use_profile`, `save_profile`).
- **Provider preference**: `set_provider` / `get_session_prefs` (agy-cli vs agy-oauth).
- **Account tools**: `whoami`, `logout` (local token forget only).
- **compare_models**: short multi-model side-by-side (max 3).
- **review_diff**: git-diff-aware Antigravity code review.
- Capacity fallback notes on chat responses when model is downgraded.
- Skills: antigravity-login, model-picker, research, pair, profiles.

## 0.9.5 - 2026-07-16

### Added

- **Model selection preferences**: save default and per-task models
  (`chat` / `code` / `fast` / `writing` / `grounded-search` / `release` / `image`).
- MCP tools: `google_antigravity_get_model_prefs`, `_set_model`,
  `_clear_model_prefs`.
- CLI: `scripts/google_antigravity_model.py` (list/get/set/clear).
- Chat, write, grounding, image, and `route_model` honor saved prefs when
  `model` is omitted; aliases like `flash`, `pro`, `opus`, `sonnet`.

## 0.9.4 - 2026-07-16

### Added

- Chat multimodal + tool-call mapping (data-URL images, OpenAI tool_calls /
  tool results → Gemini functionCall/functionResponse, function tools).
- Optional chat `stream: true` over Code Assist `streamGenerateContent` with
  MCP `notifications/message` deltas and non-stream fallback.
- Login complete **probe** (`list_models` or tiny chat) after OAuth success.
- Doctor report includes direct-login readiness.
- Skills updated for login, streaming, and agy-oauth requirements.

### Tests

- Multimodal/stream unit coverage; expanded OAuth login/error/refresh tests.

## 0.9.3 - 2026-07-16

### Added

- Direct Google Antigravity OAuth login (PKCE), ported from the Hermes
  `google-antigravity` plugin flow:
  - CLI: `python3 scripts/google_antigravity_login.py`
  - MCP: `google_antigravity_login_start` / `_complete` / `_status`
- Token refresh via OAuth client credentials when a refresh token is present.
- Auto-select `agy-oauth` when a direct login token file is available.

### Changed

- Token path resolution prefers the plugin `oauth-token.json`, then agy
  export, then Hermes auth file.

## 0.9.2 - 2026-07-16

### Added

- Documented Hermes → Codex source map in `docs/SOURCE_MAP.md`.
- Hermes OAuth file fallback path (`~/.hermes/auth/google_antigravity.json`)
  when the default agy token export is missing and no explicit path is set.
- Cloud Code PA model alias expansion and `MODEL_CAPACITY_EXHAUSTED` fallback
  chain adapted from `hermes-google-antigravity-plugin`.
- Broader static model catalog (Gemini tiers, Claude thinking, GPT-OSS).

### Changed

- Version fields bumped to `0.9.2` across package, pyproject, and Codex
  plugin manifest.

## 0.9.1 - 2026-07-15

### Changed

- Removed the Gemini Developer API and Vertex AI implementations, optional
  dependencies, configuration, tests, and documentation. Model calls now use
  only official `agy`-owned authentication paths.
- Made `agy-cli` the only automatic provider. The internal Code Assist
  `agy-oauth` compatibility transport now requires explicit selection.
- Provider diagnostics now distinguish model-catalog readiness from a verified
  live prompt, and the standalone doctor can run an opt-in `--live` check.

### Fixed

- Detached official CLI subprocess stdin from the persistent MCP transport so
  `agy models` cannot block waiting for MCP input.
- Rejected native grounding and image requests on the text-only `agy-cli`
  transport instead of silently translating them into unsupported prompts.

### Tests

- Added regression coverage for explicit provider selection, capability
  boundaries, truthful doctor outcomes, and persistent-stdin MCP operation.

## 0.9.0 - 2026-07-15

### Added

- Integrated the Antigravity-Proxy-style official `agy` refresh and JSON token
  export loader as the consent-gated `agy-oauth` provider.
- Added `google_antigravity_agy_auth_status` and
  `google_antigravity_agy_auth_refresh` without returning credential values.
- Added an `agy-cli` model provider so current macOS installations can use the
  official CLI-owned session when no reusable JSON token export exists.

### Changed

- Unified chat, writing, grounded search, model listing, image routing, and
  provider diagnostics behind deterministic provider selection.
- Provider selection now supports `gemini-api`, `vertex-ai`, `agy-oauth`, and
  `agy-cli`. Existing documented Google providers remain available unchanged.

### Security

- Token exports must be regular, non-symlink, current-user-owned files with
  mode `0600` or stricter and a bounded JSON size.
- Token values stay in memory and are excluded from representations, MCP
  results, diagnostics, and upstream error reporting.
- Current macOS Keychain entries, browser state, and CLI binaries are never
  scraped. If the official CLI does not emit a token export, the provider uses
  the official `agy` subprocess boundary instead.

## 0.8.0 - 2026-07-15

### Added

- Documented Gemini Developer API authentication through `GOOGLE_API_KEY` or
  `GEMINI_API_KEY`.
- Vertex AI support through `google-auth` Application Default Credentials,
  with explicit project, location, and optional dependency declarations.
- Provider diagnostics and deterministic configuration selection that fails on
  ambiguous or partial environments.

### Changed

- Chat, grounded search, writing, image generation, model discovery, and
  release polishing now use one explicitly selected documented Google
  provider.
- Image generation uses the official Gemini `generateContent` response
  modalities and image response-format contract.
- Model routing now recommends official Gemini API model identifiers rather
  than Antigravity-specific reasoning aliases.

### Removed

- The localhost Antigravity-Proxy runtime, proxy URL/key configuration,
  proxy-specific `generateImages` contract, and runtime-status tool.

### Security

- Fixed Google HTTPS endpoints, blocked redirects, ignored ambient proxies,
  bounded retries/responses, secret-free provider representations, and
  response-body suppression for HTTP failures.
- The provider boundary explicitly excludes Antigravity subscription auth,
  `agy` OAuth clients/tokens, browser state, Keychain entries, and proxy-owned
  credentials.

## 0.7.0 - 2026-07-15

### Added

- One internal local Gemini REST runtime for all model-facing MCP tools, using
  the documented Antigravity-Proxy `health`, model-list, content, and image
  endpoints.
- Runtime diagnostics, explicit loopback configuration, bounded retries and
  responses, and contract tests for model calls, health, redirects, and
  secret-safe HTTP failures.

### Changed

- Chat, grounded search, writing, image generation, model discovery, and
  optional release polishing now use the integrated runtime rather than a
  separate direct-auth model path.
- The README, security guide, plugin metadata, and skills now describe one
  integrated model transport and transparent runtime health diagnostics.
- Code review orchestration keeps finding discovery and technical verification
  in Codex; Antigravity may only phrase already verified records.

### Removed

- Plugin-owned Direct OAuth code, login/status MCP tools, credentials, client
  module, and user-operated authentication CLI.

### Security

- The runtime accepts only exact loopback HTTP URLs, requires an explicit local
  API key, blocks redirects, ignores ambient proxy settings, and never reads
  proxy-owned or official `agy` credential material.
- Proxy error bodies and API-key values are not returned through MCP results.

## 0.6.0 - 2026-07-15

### Added

- Dual-era MCP support for legacy sessions and the stateless `2026-07-28`
  release-candidate protocol.
- Tool titles, annotations, output schemas, deterministic discovery metadata,
  and explicit stateless workspace roots.
- macOS runtime coverage for private credential and consent permissions,
  disposable CLI directories, and bundled MCP startup from paths with spaces.

### Security

- Plan-mode CLI prompts now run in disposable directories and cannot receive a
  repository working directory.
- The agy child process no longer inherits unrelated token, secret, password,
  API-key, private-key, credential, SSH/GPG-agent, or Codex/MCP host environment
  values. The boundary applies to all official CLI subprocesses.
- Codex-hosted status avoids the nested `agy models` path that can stall under
  a read-only MCP sandbox; the standalone doctor remains the live model check.
- Read-only authentication status no longer creates a credential lock file.
- Tests isolate consent, credentials, cache, and opt-in environment state.

## 0.5.0 - 2026-07-15

### Added

- Official `agy` 1.1.2 CLI bridge, diagnostics, and native plugin manifest.
- Allowlisted release bundles with POSIX and Windows launcher variants.
- Local path, command-execution, recursion, download-size, and SSRF guards.
- CI, compatibility validation, release packaging, and security policy.

### Changed

- The official CLI is the primary runtime path.
- Direct Code Assist, OAuth, and CLI-bridge behavior is available after
  explicit user consent, with a durable local grant/revoke workflow.
