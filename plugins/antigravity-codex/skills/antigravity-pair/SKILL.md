---
name: antigravity-pair
description: "Pair programming: Codex owns local edits/tests; Antigravity provides second-opinion design, review, or alternative approaches."
---

# Antigravity Pair Mode

## Division of labor

| Actor | Does |
|-------|------|
| **Codex** | Read repo, apply patches, run tests, final decisions |
| **Antigravity** | Architecture options, review, counter-arguments, risk lists |

## Setup

```text
google_antigravity_use_profile { "name": "pair" }
```

Uses a strong model (default opus thinking) without grounding.

## Patterns

1. **Design first** — describe constraints, call `google_antigravity_chat` with task code.
2. **Diff review** — `google_antigravity_review_diff` on the working tree or staged changes.
3. **Second opinion** — paste a short design/diff summary; ask for disagreements.
4. Codex validates every suggestion against the real tree before applying.

## Boundaries

- Do not let Antigravity “own” file writes; Codex applies edits.
- Do not send secrets, keys, or full `.env` files.
- Prefer focused snippets / diffs over whole-repo dumps.
