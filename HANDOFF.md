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
- **원래 목표**: Agent Hub를 "선택적 control plane"으로 재정의하기 전에, 실패 분류를 단일 표로 통합하고 provider가 거짓을 말할 때의 봉쇄를 종류별로 검증합니다. 4주 계획의 2주차입니다.
- **현재 단계**: 2주차 두 항목을 모두 끝냈습니다. `week2/failure-classification` 브랜치에 커밋 2건(`d0c1046`, `6546669`)이 있고 push 전입니다.
- **완료**:
  - 실패 분류를 결정하던 8개 사이트를 `src/agent_hub/v2/failure_classes.py`의 `FAILURE_CLASSES` 단일 표로 통합했습니다. provider_runtime의 기본 error type, provider_worker의 payload 승격 기본값과 retryable 집합, fallback 루프의 ambiguous 집합, wave의 오류 선택과 checkpoint 파생 불리언, store의 `fallback_exhausted` 특수 케이스, finalize 3경로의 status 선택이 각각 독립적으로 판단하며 서로 어긋나 있었습니다.
  - 분류 기준을 "실패했는가"가 아니라 "응답을 받았는가"로 잡았습니다. 미발송이거나 provider가 작업을 거절한 것은 retry_safe, 보냈는데 응답이 없는 것은 ambiguous, 응답을 받고 내용상 거절된 것은 terminal입니다. 표에 없는 코드는 항상 ambiguous로 떨어지며 절대 retry_safe가 되지 않습니다.
  - 막혀 있던 3건과 동형인 실패(이름 없는 payload 실패)가 `outcome_unknown`이 되고 `prepare_reconcile`/`apply_reconcile`로 종결되는 것을 end-to-end 테스트로 확인했습니다. 2-1의 완료 기준을 충족합니다.
  - 통합 과정에서 provider_worker가 이름 없는 payload 실패에 `provider_operation_failed`를 붙여 "내용상 거절됨"으로 단정하던 결함을 찾아 고쳤습니다. 그게 막힌 run 3건의 정확한 형태입니다.
  - run이 paused인데 step은 outcome_unknown인 상태를 찾아 고쳤습니다. `prepare_reconcile`은 run이 outcome_unknown일 때만 받으므로 그 step은 재시도도 조정도 불가능했습니다. 불변식 `unresolved_steps_keep_their_run_reconcilable`을 추가해 재발을 막습니다.
  - `HubV2Error.public()`이 표가 아는 코드에 대해 표의 판정을 보고하도록 했습니다. `provider_timeout`이 wire에 `retryable=true`로 나가 이를 읽는 에이전트가 이미 전달됐을 수 있는 요청을 재전송하는 경로를 막았습니다.
  - 거짓말 15종을 파라미터화한 fault injection 스위트를 추가했습니다. 종류마다 봉쇄를 확인하고, 공통으로 모든 run이 공개 도구로 진행 가능한 상태에 도달하는지 검사합니다.
  - 스위트가 결함 3건을 드러냈고 모두 고쳤습니다. `_structured_text`가 text 없는 응답을 `json.dumps(envelope)`로 바꿔 검증을 통과시키고 step을 완료 처리하던 laundering, 진행 수단이 없는 run이 paused로 남아 존재하지 않는 재개 가능성을 주장하던 문제, provider 응답의 model 문자열이 `ensure_public_model_id`를 우회해 내부·placeholder id가 step 기록과 라우팅 버킷에 들어가던 누출입니다.
- **미완**: 3주차의 라우팅 층 삭제와 self-scored quality 폐쇄 루프 차단, 4주차의 provider MCP 중복층·sdk·workflows 삭제와 claude API 키 lane 실험이 남았습니다. 브랜치를 아직 push하지 않았습니다.
- **변경 파일**: 신규 `src/agent_hub/v2/failure_classes.py`, `tests/agent_hub/test_v2_failure_classes.py`, `tests/agent_hub/test_v2_fault_injection.py`. 수정 `src/agent_hub/v2/service.py`, `src/agent_hub/v2/provider_worker.py`, `src/agent_hub/v2/provider_runtime.py`, `src/agent_hub/v2/errors.py`, `src/agent_hub/v2/store.py`, `src/agent_hub/v2/invariants.py`, `tests/agent_hub/test_v2_service.py`, `tests/agent_hub/test_v2_provider_runtime.py`.
- **검증 실행 결과**: 전체 pytest `731 passed, 2 skipped`; `ruff check` 통과; `ruff format --check` 180 files already formatted; `./scripts/check-sync.sh` 통과입니다. 1주차 종료 시점 678건 대비 테스트 53건이 늘었습니다.
- **현재 리스크**: 동작 변경 3건이 기존 계약을 바꿉니다. `codex_process_error`와 `codex_timeout`이 retryable에서 ambiguous로 바뀌어 CLI subprocess가 죽으면 자동 재시도 대신 사람의 조정을 기다립니다. 진행 수단이 없는 run이 paused 대신 failed로 종결됩니다. text 없는 응답이 완료가 아니라 `deterministic_verification_failed`로 떨어집니다. 배포된 2.4.1 릴리스에는 1·2주차 변경이 모두 미반영입니다. 사용자 DB의 막힌 run 5건은 지시대로 손대지 않았고 이번 변경은 신규 run에만 적용됩니다.
- **Do-Not-Repeat**: 표에 없는 코드의 기본값을 retry_safe 쪽으로 바꾸지 마세요. 이미 전달됐을 수 있는 요청을 자동 재전송하는 것이 이 표가 막으려는 유일한 실수입니다. fallback 루프가 retry_safe 이외의 실패에서 다음 provider로 넘어가게 만들지 마세요. 그러면 `fallback_exhausted`의 retry_safe 분류가 즉시 거짓이 됩니다. `_structured_text`에 envelope 직렬화 fallback을 되살리지 마세요. 답변이 아닌 것을 답변으로 만듭니다. 진행 수단이 없는 run을 paused로 되돌리지 마세요. 공개 도구 14개에 replan이 없어 재개가 불가능합니다.
- **다음 한 걸음**: `src/agent_hub/v2/service.py` 707행의 `routing_mode="pinned" if explicit else "shadow"`를 읽고 라우팅 층 삭제 범위를 확정하세요.
<!-- agent-hub:handoff:v1:end -->
