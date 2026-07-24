# NOTICE

agent-hub is a unified personal multi-model coding hub. It consolidates one
substrate (instructions / handoff / memory) and four formerly-separate MIT
plugins into a single project. Each vendored package keeps its own attribution
below; the originals live at `github.com/Meapri/{orchestrate-codex,claude-codex,
grok-codex,google-antigravity-codex}`.

All model/provider calls require explicit user consent, enforced per leaf.

## orchestrate-codex (`src/orchestrate_codex`)

Original, provider-neutral orchestration MCP. No third-party source vendored.

## claude-codex (`src/claude_codex`)

1. **Anthropic Messages protocol** — ideas from
   [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT).
2. **Claude subscription plan-lane fingerprint** — adapted from
   [Meapri/hermes-claude-auth](https://github.com/Meapri/hermes-claude-auth) (MIT),
   which tracks [griffinmartin/opencode-claude-auth](https://github.com/griffinmartin/opencode-claude-auth).

Claude Code / Claude.ai subscription credentials are used outside the official
Claude Code CLI. This may break if Anthropic changes validation. Use at your own risk.

## grok-codex (`src/grok_codex`)

1. **xAI Chat Completions / Responses** — ideas from
   [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT).
2. **SuperGrok device-code OAuth** — client id, discovery, and device flow pattern
   from Hermes `xai-oauth` provider (rewritten for Codex MCP; no Hermes runtime).

xAI may restrict OAuth API access by subscription tier; API key fallback remains available.

## openai-codex (`src/openai_codex`)

The GPT provider is informed by two upstream projects:

1. [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT),
   pinned for this integration at commit
   `74a56b76b08bccc4b4a85076af15e2c176ab5542`. Agent Hub adapts the Codex
   provider profile, model discovery, Responses output normalization, and
   bounded retry/error-classification ideas. It does not vendor the Hermes
   agent loop, gateway, installer, or token store.
2. [openai/codex](https://github.com/openai/codex) (Apache-2.0), pinned for this
   integration at commit `94ebae725e5e8f22b5d86773d9223047f57b6118`.
   Agent Hub uses the public `codex app-server` JSON-RPC account/model
   interfaces and isolated `codex exec` generation so the official Codex
   installation remains the sole owner of ChatGPT OAuth tokens and refresh
   behavior.

No Codex access token, refresh token, Keychain entry, or `auth.json` payload is
read, copied, returned, or persisted by Agent Hub. See
`plugins/openai-codex/docs/SOURCE_MAP.md` for the file-level source boundary.

## google-antigravity-codex (`src/google_antigravity_codex`)

Provides an official `agy` CLI transport plus an optional compatibility transport
for an `agy`-owned Antigravity token export. It does not include or derive an OAuth
client, inspect browser state or macOS Keychain, scrape the official CLI binary, or
vendor proxy runtime code. Token values are never returned through MCP. Architecture
ideas were informed by the MIT-licensed
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) and the
authentication boundary demonstrated by
[Meapri/Antigravity-Proxy](https://github.com/Meapri/Antigravity-Proxy).
