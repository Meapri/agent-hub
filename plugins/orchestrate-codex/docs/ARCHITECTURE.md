# Architecture

## Supervised orchestration (v0.1)

1. Client calls `orchestrate_plan_recipe`.
2. Server returns ordered stages with **suggested leaf tool names** and args templates.
3. Codex (or user) executes each leaf tool and may call plan again with stage results later.

Leaf plugins remain independently usable.

## Binding defaults

```json
{
  "chat": "claude_codex_chat",
  "chat_alt": "grok_codex_chat",
  "chat_gemini": "google_antigravity_chat",
  "grounded_search": "google_grounded_search",
  "image": "google_antigravity_generate_image"
}
```
