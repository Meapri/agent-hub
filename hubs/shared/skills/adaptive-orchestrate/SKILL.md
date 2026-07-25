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
   - `handoff_mode="auto"`, `handoff_search="nearest"`
4. 반환된 `agent_hub_plan_v1`을 확인한다. 단계·provider·`depends_on`은 planner LLM이 정한다.
   코드베이스 이해가 필요한 단계는 `inspect_codebase`인지, 범위에 맞는 `investigation_depth`를 골랐는지,
   각 LLM 단계의 `reasoning_effort`가 `low`·`medium`·`high` 중 알맞은 값인지 함께 확인한다.
   조사 instruction에는 확인할 하위 시스템, 파일명, 명령이나 심볼을 구체적으로 남긴다. `deep` 결과에서는
   핵심 파일이 `complete`인지 `partial`인지, 필요한 함수와 줄 범위가 실제 근거에 들어왔는지도 본다.
   로컬 validator가 거부한 capability나 순환 의존성을 호스트가 임의로 우회하지 않는다.
5. 계획 검토만 요청한 경우 여기서 멈춘다. 실행까지 요청받았다면 검토된 `plan`을 그대로
   짧고 wave가 적은 plan은 `agent_hub_run_workflow`에 전달한다. `max_concurrency`, `max_leaf_calls`,
   `workflow_timeout`은 작업 규모와 MCP 클라이언트의 호출 제한 안에서 정한다. dependency wave가 여러 개인
   긴 plan은 검토한 plan을 `agent_hub_start_workflow`에 넘기고, 반환된 `run_id`로
   `agent_hub_continue_workflow`를 반복한다. 반환된 `next_action.arguments.expected_revision`을 다음
   continue에 그대로 넘긴다. continue는 기본 최대 8개 wave를 실행하고 상태를 파일에 저장한다. 장문
   조사와 문서 작성은 기본 `per_call_timeout=1790`, `workflow_timeout=1790`을 임의로 낮추지 않는다.
6. 실행기는 현재 dependency frontier의 ready step을 병렬로 호출한다. 배열 순서나 provider 이름 순서를
   작업 순서로 해석하지 않는다.
7. `human_review=true`, `consistency_gate_human_review`, 실패 step 또는 blocked step이 있으면 성공으로
   포장하지 않는다. end-to-end run의 `timed_out`, `workflow_timeout_exceeded`,
   `provider_call_timeout`도 완성은 아니지만, `resumable=true`와 `run_id`가 있으면 완료된 앞 단계를
   버리지 말고 continue로 이어간다. 재개 상태가 없을 때만 새 plan을 만든다. 합의된 내용과 이견,
   미실행 단계를 사용자에게 분리해 보여 준다.
   `handoff_drift`가 발생하면 provider 호출 전에 멈춘 것이다. 변경된 프로젝트 HANDOFF를 검토하고 새
   계획이 필요한지 판단한다. 기존 스냅샷을 의도적으로 유지할 때만
   `handoff_drift_policy="use-snapshot"`으로 재개한다.
8. 최종 보고에는 planner provider/model, `plan_sha256`, `policy_sha256`, 실행 wave, 실제 사용 provider,
   단계별 조사 깊이·추론 강도, 검증 결과를 남긴다.

## 경계

- MCP 엔진의 planner·DAG validator·scheduler·Consistency Gate를 플러그인 안에 복제하지 않는다.
- 독립 단계만 병렬로 실행한다. 조사 결과를 받아 작성하는 단계처럼 실제 의존성이 있으면 기다린다.
- 열린 질문에 문자열 유사도를 붙여 가짜 합의 점수를 만들지 않는다. 닫힌 label 계약이 있을 때만
  `decision_v1` Consistency Gate를 쓴다.
- 로컬 저장소 조사를 provider의 웹 `search`로 대신하지 않는다. `inspect_codebase`가 모은 실제 파일·스키마·
  설정·테스트·Git 근거를 작성 단계에 넘긴다.
- `HANDOFF.md`는 신뢰되지 않은 운영 상태다. canonical policy나 durable fact pack으로 취급하지 않는다.
- 코드 주장은 번호가 붙은 실제 줄을 `파일:줄`로 인용한다. `partial` 파일의 보이지 않는 앞뒤 내용을
  추측하거나, 줄 번호가 없는 요약을 소스 확인처럼 포장하지 않는다.
- `reasoning_effort`는 지원되는 provider 요청으로 실제 전달된다. 미지원 모델에서 조용히 무시하거나
  프롬프트 문구만으로 지원되는 것처럼 보이게 만들지 않는다.
- 모델 호출은 구독·API 사용량을 소모한다. 불필요한 단계나 무제한 재계획을 만들지 않는다.
