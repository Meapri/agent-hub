---
description: Ask Agent Hub's planner LLM for a validated adaptive DAG without executing it
argument-hint: Goal to plan
---

Call `agent_hub_plan_workflow` with:

- `workflow_id`: `adaptive`
- `prompt`: `$ARGUMENTS`
- `project_root`: the current repository's absolute path
- `policy_mode`: `required`

Show the returned steps as a dependency graph or compact table. Explain which steps can run concurrently,
which provider the planner selected, fallbacks, call budget, `plan_sha256`, and policy provenance. Do not
execute the plan unless the user explicitly asks to continue.
