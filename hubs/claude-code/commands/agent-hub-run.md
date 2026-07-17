---
description: Plan and run an adaptive Agent Hub workflow with dependency-aware parallel execution
argument-hint: Goal to orchestrate
---

Use the `adaptive-orchestrate` skill. First call `agent_hub_plan_workflow` for `$ARGUMENTS`, then pass the
reviewed plan to `agent_hub_run_workflow` with `workflow_id="adaptive"`, the current repository's absolute
`project_root`, `policy_mode="required"`, bounded `max_concurrency`, and bounded `max_leaf_calls`.

Do not manually reorder providers or replace the planner's DAG with a fixed recipe. Report any failed,
blocked, or human-review result without presenting it as completed.
