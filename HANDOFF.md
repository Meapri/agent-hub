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
- **원래 목표**: 이 저장소가 실제로 어떻게 깨져 왔는지에 근거해 Agent Hub를 개선합니다. 마지막 요청은 "출력이 자꾸 끊긴다"였습니다.
- **현재 단계**: 3.2.1을 병합(`ad85eeb`)하고 설치했습니다. daemon과 bridge 모두 `3.2.1-dfa1e6266dad`이고 doctor 7/7 pass, live canary 19 passed입니다. 진행 중인 작업은 없습니다.
- **완료**:
  - 출력 끊김의 원인은 `max_output_tokens`였고, 세 군데가 그것을 감추고 있었습니다. daemon 로그는 07-26 이후 비어 있어 근거가 못 되고 저장소 기록으로 찾았습니다. step들이 정확히 7000, 7000, 5000, 4000 output token을 보고했는데 그 run들의 `max_output_tokens`가 각각 7000, 5000, 4000이었습니다. 모델이 끝난 게 아니라 상한에 닿은 모양입니다.
  - 첫째, 잘림 신호가 버려지고 있었습니다. 어댑터는 `response.chat_outcome`에서 `finish_reason`과 `incomplete_finish_reason:max_tokens`를 만들고 envelope이 service까지 실어오는데, service.py에 `warnings`라는 단어가 아예 없었습니다. 잘린 답이 평범한 완료 step으로 저장됐습니다. `agent_hub_execute`는 envelope 전체를 돌려주므로 영향이 없었고, 그래서 durable run만 소리 없이 출력을 잃는 것처럼 보였습니다.
  - 이제 step checkpoint가 `finish_reason`·`output_truncated`·provider 경고를 담고, 잘린 step은 `step_output_truncated` 이벤트로 어떤 상한이 잘랐는지 숫자까지 남깁니다. run의 constraints는 이벤트 스트림에 없으므로 숫자가 없으면 호출자는 잘린 건 알아도 얼마로 올릴지 모릅니다. step은 계속 완료 처리합니다. 잘린 답도 답이고, 텍스트를 버리는 게 침묵보다 나쁩니다.
  - 둘째, 사용자의 답변 상한이 planner까지 목 졸랐습니다. `max_output_tokens=1500`으로 `agent_hub_plan` apply가 통째로 실패했고 300으로도 같았습니다. planner가 DAG JSON을 못 끝내고 같은 불가능한 상한으로 6번 재시도한 뒤 `plan_validation_failed`을 보고했습니다. 계획은 답변이 아니라 기계장치이므로 planner에 8192 토큰 하한을 줬습니다. 더 크게 요청하면 그대로 쓰고, run이 쓸 수 있는 총량은 여전히 `max_total_tokens`가 막습니다. planner 응답이 잘린 경우는 `planner_output_truncated`로 따로 말합니다.
  - 셋째, 잘린 답은 대개 verifier를 통과 못 해 `deterministic_verification_failed`로 끝나는데 그 실패가 잘림과 연결되지 않았습니다. 이제 그 실패가 `output_truncated`를 달고 나오고 failed step의 checkpoint까지 갑니다.
  - `max_output_tokens`에 설명이 없었습니다. 그래서 4000을 넣으면 최종 합성 step까지 전부 4000에 잘립니다. 이제 run 전체가 아니라 호출 하나당 상한이라고 말하고, 의도했을 법한 `max_total_tokens`를 가리킵니다.
  - 테스트를 쓰다가 둘을 더 잡았습니다. 이벤트 details는 allowlist를 지나므로 새 필드가 기록된 뒤 조용히 버려지고 있었고, 경고 필터에 길이 제한이 없어 provider 텍스트 200자가 코드로 통과했습니다. `finish_reason`은 provider가 정하는 값이라 런타임이 아는 닫힌 집합에서만 받습니다.
- **미완**: provider MCP 프로토콜 계층 2,228줄은 사용자 판단으로 유지합니다. gh 활성 계정 문제는 사용자가 나중에 보기로 했습니다.
- **변경 파일**: 신규 `tests/agent_hub/test_truncated_output_is_visible.py`. 수정 `src/agent_hub/v2/service.py`, `src/agent_hub/v2/store.py`, `src/agent_hub/v2/provider_runtime.py`, `src/agent_hub/v2/tools.py`, 버전 문자열 6곳.
- **검증 실행 결과**: 전체 pytest `893 passed, 2 skipped`; `ruff check` 통과; `./scripts/check-sync.sh` 통과; README verify·document_quality 통과; 공개 도구 14개 유지. 고침을 갈래별로 되돌려 각자의 테스트가 깨지는 것을 확인했습니다. 설치 후 실제 daemon에 `max_output_tokens=1500`으로 같은 요청을 다시 보내 apply가 5단계로 성공하고, `outline_article`이 정확히 1500 토큰에서 잘리며 `step_output_truncated {max_output_tokens: 1500}` 이벤트가 남는 것을 확인했습니다. live canary 19 passed(96초), doctor 7/7 pass.
- **현재 리스크**: 잘림이 심하면 step이 verifier에서 실패하고 run이 `outcome_unknown`으로 갑니다. 이제 원인은 보이지만 자동 회복은 아니고 사람이 상한을 올려 다시 돌려야 합니다. planner 하한 8192는 근거 있는 값이지만 측정한 값은 아닙니다. 12단계 DAG JSON이 그보다 커지면 다시 잘릴 수 있습니다. 3.2.0의 `planner_egress_violation` 고침은 실제 호출로 한 번만 확인했고 반복 검증은 아직입니다.
- **Do-Not-Repeat**: provider가 보낸 신호를 서비스 경계에서 버리지 마세요. 어댑터가 옳게 판단해도 호출자에게 닿지 않으면 없는 것과 같습니다. 사용자의 답변 상한을 내부 기계장치(planner)에 그대로 물리지 마세요. 이벤트에 새 필드를 넣을 때 `_SAFE_EVENT_FIELDS` allowlist를 함께 갱신하세요. 안 그러면 기록은 되고 조용히 버려집니다. provider가 정하는 문자열을 공개 필드에 그대로 싣지 마세요. 닫힌 집합이나 길이 제한 있는 패턴만 통과시키세요. 모델에게 알려주는 제약과 런타임이 심판하는 제약을 따로 두지 마세요. 인자 모양 오류를 맨 `ValueError`로 던지지 마세요. 공개 도구의 인자에 설명을 빼지 마세요. 작업 중인 파일을 `git checkout`으로 되돌리지 마세요. 복사본을 만들어 그걸로 되돌리세요.
- **다음 한 걸음**: 며칠 뒤 `sqlite3 ~/.agent-hub/state.sqlite3 "select event_type, count(*) from events where event_type='step_output_truncated' group by 1"`와 `select operation, error_code, count(*) from operation_metrics where success=0 and recorded_at > 1785400000 group by 1,2`를 함께 확인해, 잘림이 실제로 얼마나 자주 나는지와 `planner_egress_violation`이 사라졌는지 판정하세요.
<!-- agent-hub:handoff:v1:end -->
