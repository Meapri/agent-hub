---
description: Prepare an Agent Hub v2 egress manifest and validated plan without executing it
argument-hint: Goal to plan
---

Use the `adaptive-orchestrate` skill for `$ARGUMENTS`.

1. Call `agent_hub_plan` with `mode="prepare"`, a `task_v2`, the current repository's absolute
   `project_root`, and only the source paths required for the goal.
2. Show the egress entries, redactions, token budget, manifest digest, proposal digest, and policy revision.
3. If repository or artifact entries are present, stop at the local GUI action and let the user approve
   or reject the review. Do not approve it through MCP.
4. After GUI approval, call apply with the unchanged proposal, `proposal_sha256`,
   `expected_policy_revision`, and `approval_request.review_id` as `approval_request_id`; show the
   resulting `plan_v2` DAG, verifier, provider choices, fallbacks, routing mode, and digests.

Do not start the run unless the user explicitly asks to continue.
