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
3. 포함 파일, 데이터 분류, 제외된 secret 후보와 예상 예산이 맞을 때만 같은 proposal을
   `agent_hub_plan`의 `mode="apply"`에 넘긴다. `proposal_sha256`과
   `expected_policy_revision`이 일치하지 않으면 새로 prepare한다.
4. 반환된 `plan_v2`에서 DAG, capability, verifier, budget, egress digest를 검토한다. 로컬 validator가
   거부한 순환, 고립 step, capability 부족, 예산 초과를 호스트가 우회하지 않는다.
5. 실행 승인을 받으면 plan을 `agent_hub_start`에 넘긴다. 재시도해도 같은 run을 찾을 수 있도록 안정적인
   `idempotency_key`를 사용한다.
6. `agent_hub_continue`에 `run_id`와 최신 `expected_revision`을 전달한다. receipt는 외부 생성 완료를
   기다리지 않고 반환된다. `agent_hub_get`과 `agent_hub_events`로 진행을 확인하고, 활성 lease가 있는
   동안 continue를 중복 호출하지 않는다.
7. 결과 본문은 event가 아니라 `agent_hub_artifact`로 가져온다. digest와 검증 결과를 확인한 뒤 호스트가
   필요한 파일 변경을 수행한다.
8. 사용자 평가나 deterministic gate 결과가 있으면 `agent_hub_feedback`으로 기록한다. LLM 자기 평가는
   ground truth로 기록하지 않는다.

## 라우팅과 재개

- `legacy`, `shadow`, `advisory`는 planner의 실제 provider 선택을 바꾸지 않는다.
- `auto`도 정확한 context 표본이 20건 미만이면 planner 선택을 유지한다.
- 완료된 step은 재계획으로 바꾸지 않는다. fallback 소진, timeout, context limit, deterministic
  verification 실패, capability 변화에서만 미완료 subgraph를 교체한다.
- `outcome_unknown`, 인증·동의 문제, HANDOFF drift, 사용자 취소, 전체 예산 소진은 자동 재호출하거나
  자동 재계획하지 않는다.
- `agent_hub_cancel`은 새 결과의 반영을 막지만 이미 외부 provider에 전송된 요청을 되돌리지 못할 수 있다.

## 보고할 근거

최종 보고에는 plan/policy/egress digest, run ID와 revision, 실제 provider와 model, routing mode와
표본 수, 완료·실패 step, artifact ID와 검증 결과를 남긴다. prompt, token, 원문 결과나 raw exception은
event, HANDOFF, telemetry에 복사하지 않는다.
