---
name: orchestrate
description: "You (the Codex host) orchestrate Claude/Grok/Gemini leaf models: advise → plan → step → verify. Also supervised recipes and an autonomous broker."
---

# Orchestrate (v0.5)

**You are the orchestrator.** In the Codex GUI app the host model decides the plan —
which sub-tasks to run, which leaf model handles each, and what to do yourself. This
plugin supplies the routing brief, prepares each delegated call (latest model + context
+ policy), and guards quality. It does not make the judgment for you.

## Host-driven flow (default)

1. `orchestrate_advise` — get the routing brief: available leaves, **latest confirmed
   model ids** (leaf catalogs are stale — trust this, not the leaf defaults), a strength
   guide (what each model is good for), doc-class policies, and what to do yourself.
2. **You plan.** Decide the sub-tasks, assign a model to each, and do the rest directly.
3. For each delegation: `orchestrate_step { capability, instruction, doc_class, gather?,
   context?, write_task?, model? }` → returns `{tool, arguments, fallback_tools,
   verify_after}` with the latest model and injected context already applied.
4. Call that `tool` with `arguments`. On failure, try the next `fallback_tools` entry.
5. If `verify_after`, pass the result to `orchestrate_verify { text, doc_class,
   project_root }`; on warnings, re-delegate a corrected step.
6. Combine the results yourself (pass earlier findings forward via `context`).

### Example plan (README) — you decide this, not the recipe
- `orchestrate_step` chat → **Claude** (opus): "analyze architecture", `gather:"code"`.
- `orchestrate_step` chat → **Grok**: "analyze install/usage/tools", `gather:"code"`.
- `orchestrate_step` write → **Gemini**: "synthesize README", `context:<both findings>`,
  `write_task:"readme"`, `doc_class:"durable"`.
- `orchestrate_verify` the draft; re-run the write step if it warns.

## Routing guide (defaults — override with your judgment)
- **Claude (opus)** — depth, architecture, rigorous reasoning/review.
- **Grok** — fast, broad, a distinct second opinion.
- **Gemini write** — final structured authoring (readme/pr/release/translate); self-grounds.
- **grounded_search** — current/external facts with sources.
- **image / review_diff / compare** — images, diff review, multi-model comparison.
- **Do yourself**: planning, file edits, commands/tests, simple transforms.

## Also available
- **Recipes** (proven patterns): `orchestrate_start_run` → next_action → leaf → `orchestrate_continue_recipe`. Recipes include `deep_readme` (multi-LLM), `durable_readme`, `change_pr`, `research_brief`, `review_diff`, `translate_doc`, … (`orchestrate_list_recipes`).
- **Autonomous broker**: `orchestrate_run` runs a recipe end-to-end by spawning leaf servers itself (needs `leaves.json`; `orchestrate_check_leaves` to preflight).
- **`orchestrate_probe_models`** — live-confirm the latest working model id per leaf.

## Do not
- Trust leaf `list_models` for "latest" — it's stale; use `orchestrate_advise` / `orchestrate_probe_models`.
- Send a raw `prompt` to the write leaf — it takes `task`/`instruction`/`source_text` (orchestrate_step handles this).
- Put session-diary/recency tone into durable docs.
