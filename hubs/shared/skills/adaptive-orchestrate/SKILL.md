---
name: adaptive-orchestrate
description: >
  복잡한 목표를 Agent Hub planner LLM이 작업 DAG로 나누고, 의존성이 없는 단계는 병렬 실행하며,
  여러 provider 결과를 검증·합의한다. Trigger when the user asks Agent Hub to decide how to split work,
  coordinate multiple models, run independent work in parallel, or resolve cross-model disagreement.
---

# Adaptive Orchestration

Agent Hub MCP가 공통 두뇌다. 이 플러그인은 호스트 앱의 콕핏 역할만 하며 provider별 계획이나 순서를
직접 하드코딩하지 않는다.

## 실행 원칙

1. 작업 루트와 목표를 확인한다. `AGENTS.md` 또는 `CLAUDE.md`가 있으면 정본 정책으로 사용한다.
2. `agent_hub_status`로 필요한 provider가 준비됐는지 확인한다.
3. 먼저 `agent_hub_plan_workflow`를 아래 핵심 인자로 호출한다.
   - `workflow_id="adaptive"`
   - `prompt=<사용자 목표>`
   - `project_root=<절대경로>`
   - `policy_mode="required"`
4. 반환된 `agent_hub_plan_v1`을 확인한다. 단계·provider·`depends_on`은 planner LLM이 정한다.
   로컬 validator가 거부한 capability나 순환 의존성을 호스트가 임의로 우회하지 않는다.
5. 계획 검토만 요청한 경우 여기서 멈춘다. 실행까지 요청받았다면 검토된 `plan`을 그대로
   `agent_hub_run_workflow`에 전달한다. `max_concurrency`와 `max_leaf_calls`는 작업 규모에 맞게 제한한다.
6. 실행기는 현재 dependency frontier의 ready step을 병렬로 호출한다. 배열 순서나 provider 이름 순서를
   작업 순서로 해석하지 않는다.
7. `human_review=true`, `consistency_gate_human_review`, 실패 step 또는 blocked step이 있으면 성공으로
   포장하지 않는다. 합의된 내용과 이견, 미실행 단계를 사용자에게 분리해 보여 준다.
8. 최종 보고에는 planner provider/model, `plan_sha256`, `policy_sha256`, 실행 wave, 실제 사용 provider,
   검증 결과를 남긴다.

## 경계

- MCP 엔진의 planner·DAG validator·scheduler·Consistency Gate를 플러그인 안에 복제하지 않는다.
- 독립 단계만 병렬로 실행한다. 조사 결과를 받아 작성하는 단계처럼 실제 의존성이 있으면 기다린다.
- 열린 질문에 문자열 유사도를 붙여 가짜 합의 점수를 만들지 않는다. 닫힌 label 계약이 있을 때만
  `decision_v1` Consistency Gate를 쓴다.
- 모델 호출은 구독·API 사용량을 소모한다. 불필요한 단계나 무제한 재계획을 만들지 않는다.
