# HANDOFF — Agent Hub

> 이건 요약이 아니다. **다음 에이전트(어느 하네스든)를 위한 복구 기록**이다.
> 현재 구조와 사용법은 [`README.md`](./README.md), 실행 계약은
> [`docs/architecture/agent-hub-v2-protocol.md`](./docs/architecture/agent-hub-v2-protocol.md)를 기준으로 합니다.
> 현재 작업 상태와 다음 한 걸음은 아래 `agent-hub:handoff:v1` managed block이 최신입니다.

- **원래 목표**
  여러 AI 코딩 에이전트(Claude Code · Codex/ChatGPT · Antigravity CLI · Grok · Cursor)를 한 사람이 쓸 때
  **작업 플로우가 끊기지 않고(핸드오프) 모델 성향과 무관하게 일관적으로 작동하는** 개인용 시스템을 이 레포에 구축한다.
  정본 원칙: 모든 상태는 Git에 커밋되는 파일에 산다. 도구는 소모품이다.

<!-- agent-hub:handoff:v1:start -->
- **원래 목표**: 이 저장소가 실제로 어떻게 깨져 왔는지에 근거해 Agent Hub를 개선합니다.
- **현재 단계**: 3.2.0을 병합(`0e59b94`)하고 설치했습니다. daemon과 bridge 모두 `3.2.0-3b8aa551dd52`이고 doctor 7/7 pass, store schema 11입니다. 진행 중인 작업은 없습니다.
- **완료**:
  - `operation_metrics`에서 살아 있는 실패만 골라 세 가지를 고쳤습니다. `error_code`가 채워지기 전(07-27 이전) 39건은 컬럼 추가 전 NULL이라 근거로 쓰지 않았습니다.
  - planner에게 A를 알려주고 B로 심판하고 있었습니다. 프롬프트가 `하드 제약`이라 이름 붙여 보여주는 provider 목록은 정적 `CAPABILITY_PROVIDERS` 표(claude·grok·gemini·gpt)이고 `fallback_providers`까지 권했는데, 실행 시 심판하는 것은 그 run의 egress 승인 목록이고 기본값은 요청한 provider 하나뿐이었습니다. 지시를 따른 계획이 통째로 버려졌고 `planner_egress_violation`은 `terminal`이라 재시도도 없었습니다. 6건.
  - 이게 모델 탓이 아니라 프롬프트 탓이라는 방증: 프롬프트가 명시하는 capability 제약 위반은 같은 기간 0건입니다.
  - 승인 목록을 worker까지 전달해 프롬프트가 그 run이 실제로 닿을 수 있는 provider만 제시하고, 승인된 provider가 아무도 못 하는 capability는 아예 제시하지 않으며, `validate_plan`이 로컬에서 걸러 이미 있던 planner repair 루프가 승인 목록을 알려주며 회복합니다. service의 fence는 그대로 두어 여전히 최종 권한입니다.
  - `durable_run_required` 5건. execute가 기록형 작업을 거절하면서 `record`인지 `task.retention`인지 말하지 않았고 두 필드 모두 설명이 없었습니다. 둘 다 설명하고, 어느 쪽이 걸렸는지 이름을 대고, 도구를 바꾸는 것 말고 그 필드를 빼도 된다고 알려줍니다.
  - `invalid_egress_proposal` 4건과 `proposal_digest_conflict` 4건. apply는 prepare 결과 전체가 필요한데 메시지가 `incomplete`, `does not match`뿐이었습니다. 빠진 키 이름을 대고 digest를 다시 계산하거나 proposal을 고치지 말라고 말합니다. 메시지는 authored literal과 닫힌 키 이름으로만 조립하며, proposal 내용이 새어나가지 않는지 테스트합니다.
- **미완**: provider MCP 프로토콜 계층 2,228줄은 사용자 판단으로 유지합니다. gh 활성 계정 문제는 사용자가 나중에 보기로 했습니다.
- **변경 파일**: 신규 `tests/agent_hub/test_planner_approved_destinations.py`, `tests/agent_hub/test_refusal_names_the_argument.py`. 수정 `src/agent_hub/orchestrator.py`, `src/agent_hub/v2/provider_runtime.py`, `src/agent_hub/v2/provider_worker.py`, `src/agent_hub/v2/service.py`, `src/agent_hub/v2/tools.py`, `src/agent_hub/v2/egress.py`, 버전 문자열 6곳.
- **검증 실행 결과**: 전체 pytest `873 passed, 2 skipped`; `ruff check` 통과; `./scripts/check-sync.sh` 통과; README verify·document_quality 통과; 공개 도구 14개 유지. 고침을 네 갈래로 나눠 되돌려 각자의 테스트가 깨지는 것을 확인했습니다. 설치 후 `AGENT_HUB_LIVE=1 pytest -m live` 19 passed(106초), doctor 7/7 pass. 실제 daemon에 `record=true`와 proposal 없는 apply를 보내 필드 이름이 담긴 메시지를 확인했고, 승인 destination을 `['claude']` 하나로 좁힌 plan prepare→apply를 실제 planner로 통과시켜 4단계 계획을 받았습니다.
- **현재 리스크**: 승인 destination이 좁은 plan을 실제로 한 번 통과시킨 것이지 반복 검증은 아닙니다. planner가 우연히 맞췄을 가능성은 남고, 며칠간 `planner_egress_violation`이 다시 나오는지 봐야 확정됩니다. `additionalProperties: false`인 handoff 인자는 문서에 없는 키를 호출자 쪽에서 거절하므로 새 인자를 추가할 때 문서를 함께 넣어야 합니다. live canary는 CI에서 돌지 않아 사람이 때때로 돌려야 하고, 로그인이 만료된 provider는 실패가 아니라 건너뛰므로 전부 건너뜀을 전부 통과로 오해하면 안 됩니다.
- **Do-Not-Repeat**: 모델에게 알려주는 제약과 런타임이 심판하는 제약을 따로 두지 마세요. 모델이 지시를 따를수록 실패합니다. 인자 모양 오류를 맨 `ValueError`로 던지지 마세요. 호출자에게 `internal_error`로 도착합니다. 거절 메시지에 무엇을 고쳐야 하는지 필드 이름을 빼지 마세요. 공개 도구의 중첩 인자를 빈 object로 두지 마세요. MCP 호출자는 스키마 말고는 볼 것이 없습니다. 응답의 의미를 어댑터에서 다시 정하지 마세요. 스키마 설명을 강제 코드 검증 없이 추가하지 마세요. 하위 디렉터리 conftest의 `pytest_collection_modifyitems`에서 범위 검사를 빼지 마세요. 죽은 코드를 AST만 보고 지우지 마세요. **작업 중인 파일을 `git checkout`으로 되돌리지 마세요. 실험용 수정과 함께 진짜 작업까지 사라집니다. 이 세션에서 두 번 겪었고 두 번째는 `provider_runtime.py` 전체를 다시 썼습니다. 임시 수정은 파일을 복사해 두고 복사본으로 되돌리세요.**
- **다음 한 걸음**: 며칠 뒤 `sqlite3 ~/.agent-hub/state.sqlite3 "select operation, error_code, count(*) from operation_metrics where success=0 and recorded_at > 1785300000 group by 1,2"`로 `planner_egress_violation`과 `durable_run_required`가 실제로 사라졌는지 확인하세요.
<!-- agent-hub:handoff:v1:end -->
