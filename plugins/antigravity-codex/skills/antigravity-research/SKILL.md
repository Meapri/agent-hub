---
name: antigravity-research
description: "Current-facts research with Antigravity: research profile, grounded search, source-aware answers."
---

# Antigravity Research

## Prerequisites

- Consent granted
- **agy-oauth** (direct login or token export) — grounding needs it
- Prefer profile: `google_antigravity_use_profile` `{ "name": "research" }`

## Workflow

1. Confirm login: `google_antigravity_login_status` / `whoami` if needed.
2. Activate research profile (sets grounding + model + prefers agy-oauth).
3. `google_grounded_search` for source-backed questions.
4. Optionally `google_antigravity_chat` with `grounding: "always"` for multi-turn synthesis.
5. Present claims with URLs; flag thin evidence.

## Do not

- Treat plugin OAuth only path as grounded search.
- Invent citations.
