---
name: adaptive-orchestrate
description: >
  복잡한 목표를 Agent Hub planner가 검증 가능한 DAG로 나누고 durable run으로 실행한다.
  Trigger when the user asks Agent Hub to plan, coordinate multiple providers, execute independent
  work in parallel, or resume a long-running workflow.
---

# Adaptive Orchestration

Agent Hub daemon이 계획, 정책 검증, 라우팅, 실행 상태를 소유한다. 호스트 플러그인은 provider 순서나
실행 스케줄을 복제하지 않고 v2의 14개 공개 도구만 사용한다.

## 실행 순서

1. 작업 루트와 목표를 확인하고 `agent_hub_status`로 daemon, DB, provider 상태를 읽는다.
2. 저장소 파일이나 기존 artifact를 외부 planner에 보낼 계획이면 `agent_hub_plan`을
   `mode="prepare"`로 먼저 호출한다. `task_v2`, 절대 `project_root`, 필요한 `source_paths`를 넘기고
   반환된 fact pack, `egress_manifest_v2`, `proposal_sha256`, policy revision을 검토한다. prepare는
   provider를 호출하지 않는다.
3. 저장소 파일이나 기존 artifact가 있으면 `approval_mode`를 확인한다. `manual`이면
   `next_action`의 로컬 연결 GUI를 열어 사용자가 전송 대상과 파일 목록을 승인하거나 거부하게
   한다. `automatic`이고 review 상태가 `approved`이면 전역 자동 승인 기록을 그대로 사용한다.
   어느 경우든 같은 proposal과 `approval_request.review_id`를 `approval_request_id`로
   `agent_hub_plan(mode="apply")`에 넘긴다.
   `proposal_sha256`과 `expected_policy_revision`이 일치하지 않거나 review가 만료되면 새로
   prepare한다. MCP 호출자가 review를 대신 승인하려고 하지 않는다.
4. 반환된 `plan_v2`에서 DAG, capability, verifier, budget, egress digest를 검토한다. 로컬 validator가
   거부한 순환, 고립 step, capability 부족, 예산 초과를 호스트가 우회하지 않는다.
5. 실행 승인을 받으면 plan을 `agent_hub_start`에 넘긴다. 재시도해도 같은 run을 찾을 수 있도록 안정적인
   `idempotency_key`를 사용한다.
6. `agent_hub_continue`에 `run_id`와 최신 `expected_revision`을 전달한다. receipt는 외부 생성 완료를
   기다리지 않고 반환된다. `agent_hub_get`과 `agent_hub_events`로 진행을 확인하고, 활성 lease가 있는
   동안 continue를 중복 호출하지 않는다. paused run에서 `agent_hub_get`이
   `retryable_failed_steps`와 `next_action`을 반환하면 사용자가 재시도를 원할 때만 그 최신
   revision과 step 목록을 `retry_failed_steps`로 전달한다.
7. 결과 본문은 event가 아니라 `agent_hub_artifact`로 가져온다. digest와 검증 결과를 확인한 뒤 호스트가
   필요한 파일 변경을 수행한다.
8. 사용자 평가나 deterministic gate 결과가 있으면 `agent_hub_feedback`으로 기록한다. LLM 자기 평가는
   ground truth로 기록하지 않는다.

## 라우팅과 재개

- `pinned`는 선택한 provider가 실행 불가능하면 fallback으로 바꾸지 않고 실패한다.
- `shadow`와 `advisory`는 planner provider가 capability, policy, readiness와 context 검사를
  통과할 때 선택을 유지한다. 부적격 provider는 eligible fallback으로 바뀔 수 있다.
- `auto`는 관측 표본 하한, prior 지분 상한, 두 후보의 점수 분리 조건을 모두 만족할 때만 planner
  선택을 바꾼다. 사용자가 적어 둔 prior만으로는 provider가 바뀌지 않는다.
- 완료된 step은 재계획으로 바꾸지 않는다. fallback 소진, timeout, context limit, deterministic
  verification 실패, capability 변화에서만 미완료 subgraph를 교체한다.
- 내부 오류, 인증·동의 문제, HANDOFF drift, 사용자 취소는 명시적 retry 목록에도 넣지 않고
  자동 재호출하거나 자동 재계획하지 않는다.
- run이 `run_token_budget_exhausted`로 멈추면 남은 step은 보존된다. 사용자가 예산 추가를 승인할
  때만 `agent_hub_continue`에 `token_budget_grant`를 전달한다. 금액은 `agent_hub_get`의
  `token_usage`와 `next_action`을 근거로 제시한다.
- `outcome_unknown`은 자동으로 재시도하지 않는다. 사용자가 외부 요청의 실제 전달 여부를 판단해야
  하므로, `agent_hub_cancel`의 `prepare_reconcile`로 판정안을 만들어 사용자에게 보여 주고 승인을
  받은 뒤에만 `apply_reconcile`을 호출한다. `agent_hub_get`의 `next_action`은 재전송하지 않는
  `delivered_discarded`만 미리 채워 두므로, 재전송을 뜻하는 `not_delivered`로 바꾸는 것은 사용자가
  명시적으로 요청했을 때만 한다.
- `agent_hub_cancel`의 취소는 새 결과의 반영을 막지만 이미 외부 provider에 전송된 요청을 되돌리지
  못할 수 있다.

## 보고할 근거

최종 보고에는 plan/policy/egress digest, run ID와 revision, 실제 provider와 model, routing mode와
표본 수, 완료·실패 step, artifact ID와 검증 결과를 남긴다. prompt, token, 원문 결과나 raw exception은
event, HANDOFF, telemetry에 복사하지 않는다.
