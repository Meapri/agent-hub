---
name: claude-codex
description: "Use the Claude Codex leaf MCP for consent-gated Anthropic chat, model listing, and provider status."
---

# Claude Codex

1. Confirm consent (`claude_codex_consent_status` / grant script).
2. Ensure `ANTHROPIC_API_KEY` is set (`claude_codex_provider_status`).
3. Use `claude_codex_chat` for prose/code reasoning.
4. Prefer this leaf for direct Claude calls; multi-step docs pipelines belong in an orchestrator plugin.
