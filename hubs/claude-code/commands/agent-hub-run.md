---
description: Plan and start an Agent Hub v2 durable run with revision-fenced execution
argument-hint: Goal to orchestrate
---

Use the `adaptive-orchestrate` skill for `$ARGUMENTS`.

Call `agent_hub_plan` with the current repository's absolute `project_root` to prepare and review the
egress proposal. Apply it to obtain `plan_v2`, then call `agent_hub_start` with the same `project_root`
and a stable `idempotency_key`. Continue with the latest `expected_revision`. The receipt should return
before provider generation finishes; use `agent_hub_get` and `agent_hub_events` to follow progress and
`agent_hub_artifact` to retrieve results.

Do not duplicate an active lease, silently retry `outcome_unknown`, or present failed, blocked, cancelled,
unverified, or approval-waiting work as complete.
