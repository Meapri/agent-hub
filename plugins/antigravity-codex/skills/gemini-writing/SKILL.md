---
name: gemini-writing
description: "Use consent-gated Antigravity writing for drafting, rewriting, polishing, translating, summaries, PR descriptions, release notes, and documentation."
---

# Gemini Writing

After confirming explicit consent, use `google_antigravity_write` for prose
deliverables. Keep code implementation outside the writing pass.

## Doc class (important)

| task | class | behavior |
| --- | --- | --- |
| `readme`, `technical-doc` | **durable** | Fact pack auto; **git diary forced off**; no session-work tone |
| `pr-description`, `release-notes` | **change** | `project_context=auto` → git ok |
| polish/translate/… | **transform** | source-first; git off by default |

For multi-step README (outline → draft → verify, fallbacks across Claude/Grok/AG),
prefer **orchestrate-codex** `durable_readme` instead of a single write call.

## Defaults

- Durable tasks: pass `project_root` when possible; do not set `project_context` to git.
- Change tasks: `project_context=auto` or `git-diff` as needed.
- Verify versions, commands, and tool names against the fact pack / source before publishing.
