---
description: Prepare an Agent Hub v2 egress manifest and validated plan without executing it
argument-hint: Goal to plan
---

Use the `adaptive-orchestrate` skill for `$ARGUMENTS`.

1. Call `agent_hub_plan` with `mode="prepare"`, a `task_v2`, the current repository's absolute
   `project_root`, and only the source paths required for the goal.
2. Show the egress entries, redactions, token budget, manifest digest, proposal digest, and policy revision.
3. Do not call `mode="apply"` until the proposal is approved.
4. If approved, call apply with the unchanged proposal, `proposal_sha256`, and
   `expected_policy_revision`; show the resulting `plan_v2` DAG, verifier, provider choices, fallbacks,
   routing mode, and digests.

Do not start the run unless the user explicitly asks to continue.
