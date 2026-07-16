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

## google-antigravity-codex (`src/google_antigravity_codex`)

Provides an official `agy` CLI transport plus an optional compatibility transport
for an `agy`-owned Antigravity token export. It does not include or derive an OAuth
client, inspect browser state or macOS Keychain, scrape the official CLI binary, or
vendor proxy runtime code. Token values are never returned through MCP. Architecture
ideas were informed by the MIT-licensed
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) and the
authentication boundary demonstrated by
[Meapri/Antigravity-Proxy](https://github.com/Meapri/Antigravity-Proxy).
