# GPT provider source map

This file records the upstream material consulted for Agent Hub's internal GPT
provider adapter and the boundary between adapted protocol ideas and code
written in this repository. It does not describe a separately installable
provider: the public surface is the unified `agent_hub_*` API with provider
ID `gpt`.

## Pinned upstream revisions

- NousResearch/hermes-agent
  - commit: `74a56b76b08bccc4b4a85076af15e2c176ab5542`
  - license: MIT
- openai/codex
  - commit: `94ebae725e5e8f22b5d86773d9223047f57b6118`
  - license: Apache-2.0

## Consulted Hermes files

| Upstream path | Idea used by Agent Hub |
| --- | --- |
| `hermes_cli/auth.py` | Separate subscription-auth provider boundary and safe status/error categories |
| `hermes_cli/codex_models.py` | Live model discovery and public catalog normalization |
| `agent/transports/codex.py` | Codex Responses request/response lifecycle and quota classification |
| `agent/codex_responses_adapter.py` | Final text and public usage normalization |
| `plugins/model-providers/openai-codex/*` | Provider naming, capability scope, and configuration shape |

Agent Hub does not copy Hermes's OAuth client, token persistence, agent loop,
gateway, monkey patches, installer, or shell integration. In particular, it
does not share or rotate credentials from `~/.codex/auth.json`.

## Consulted OpenAI Codex files

| Upstream path | Interface used by Agent Hub |
| --- | --- |
| `codex-rs/app-server/README.md` | Stable JSONL JSON-RPC handshake, account, and model contracts |
| `codex-rs/app-server-protocol/src/protocol/common.rs` | Stable method names and request/notification envelopes |
| `codex-cli` exec interface | Ephemeral, prompt-via-stdin generation and JSONL completion events |

The implementation launches the user's installed `codex app-server --stdio`
without a shell for redacted account status and model discovery. Generation
runs `codex exec` in an isolated temporary directory with user configuration,
repository rules, MCPs, shell tools, and web search disabled. Codex owns login
state, token refresh, and upstream API requests. Agent Hub fails closed on
side-effecting turn items and never exposes account email or credential
material through MCP.

## Local implementation mapping

| Agent Hub path | Responsibility |
| --- | --- |
| `src/openai_codex/client.py` | Bounded app-server and isolated exec transports |
| `src/openai_codex/auth.py` | Redacted account status and external login guidance |
| `src/openai_codex/models.py` | Live signed-in model discovery |
| `src/openai_codex/chat.py` | Ephemeral read-only generation and final result normalization |
| `src/openai_codex/mcp_server.py` | Private leaf tools and MCP-compatible dispatch |
| `src/agent_hub/providers/openai.py` | Agent Hub provider adapter |
