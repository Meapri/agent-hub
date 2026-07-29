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
- **원래 목표**: 이 저장소가 실제로 어떻게 깨져 왔는지에 근거해 Agent Hub를 개선합니다. 마지막 요청은 "출력이 자꾸 끊긴다"였고, 이어서 그 원인이던 `max_output_tokens`를 빼달라는 요청이었습니다.
- **현재 단계**: 3.2.2를 병합(`a0ae270`)하고 설치했습니다. daemon과 bridge 모두 `3.2.2-ef9833949c6f`, doctor 7/7 pass, live canary 19 passed입니다. 진행 중인 작업은 없습니다.
- **완료**:
  - 출력 끊김의 원인은 `max_output_tokens`였습니다. daemon 로그는 07-26 이후 비어 있어 근거가 못 됐고 저장소 기록으로 찾았습니다. step들이 정확히 7000, 7000, 5000, 4000 output token을 보고했는데 그 run들의 `max_output_tokens`가 각각 7000, 5000, 4000이었습니다.
  - 잘림 신호가 서비스 경계에서 버려지고 있었습니다. 어댑터는 `response.chat_outcome`에서 `finish_reason`과 `incomplete_finish_reason:max_tokens`를 만들고 envelope이 실어오는데 `service.py`에 `warnings`라는 단어가 아예 없었습니다. `agent_hub_execute`는 envelope 전체를 돌려주므로 영향이 없었고 그래서 durable run만 조용히 출력을 잃는 것처럼 보였습니다. 이제 step checkpoint가 `finish_reason`·`output_truncated`·provider 경고를 담고 `step_output_truncated` 이벤트가 어떤 상한이 잘랐는지 숫자까지 남깁니다. step은 계속 완료 처리합니다. 잘린 답도 답이고 텍스트를 버리는 게 침묵보다 나쁩니다.
  - 사용자의 답변 상한이 planner까지 목 졸랐습니다. `max_output_tokens=1500`으로 `agent_hub_plan` apply가 통째로 실패했고 300으로도 같았습니다. planner가 DAG JSON을 못 끝내고 같은 상한으로 6번 재시도한 뒤 `plan_validation_failed`을 보고했습니다. 계획은 답변이 아니라 기계장치이므로 planner에 8192 하한을 줬고, planner 응답이 잘린 경우는 `planner_output_truncated`로 따로 말합니다.
  - 잘린 답은 대개 verifier를 통과 못 해 `deterministic_verification_failed`로 끝나는데 그 실패가 잘림과 연결되지 않았습니다. 이제 그 실패가 `output_truncated`를 달고 failed step의 checkpoint까지 갑니다.
  - 그 상한을 저장소가 시키는 곳은 없었습니다. 호출하는 에이전트가 스스로 고른 숫자였고, 설명 없는 정수 knob은 원래 그런 추측을 부릅니다. 그래서 `max_output_tokens`와 별칭 `max_tokens`를 공개 task constraints 스키마에서 뺐습니다. `max_total_tokens`만 남기고 "이게 지정할 토큰 한도"라고 적었습니다. 총량은 다 쓰면 run이 멈추고 남은 단계가 보존되지만, 한 번의 출력 상한은 답변을 그 자리에서 자릅니다.
  - 처음에는 정책 경계에서 그 필드를 아예 거절했는데 **GUI 연결 테스트가 그걸 잡아냈습니다.** 연결 probe는 `max_tokens=512`로 싼 응답을 원하고 잘려도 상관하지 않습니다. 그게 per-call 상한의 유일하게 정직한 용도라 런타임은 계속 받습니다. 없앤 것은 권유뿐입니다.
  - 테스트를 쓰다가 둘을 더 잡았습니다. 이벤트 details는 allowlist를 지나서 새 필드가 기록된 뒤 조용히 버려지고 있었고, 경고 필터에 길이 제한이 없어 provider 텍스트 200자가 코드로 통과했습니다.
- **미완**: provider MCP 프로토콜 계층 2,228줄은 사용자 판단으로 유지합니다. gh 활성 계정 문제는 사용자가 나중에 보기로 했습니다.
- **변경 파일**: 신규 `tests/agent_hub/test_truncated_output_is_visible.py`. 수정 `src/agent_hub/v2/service.py`, `src/agent_hub/v2/store.py`, `src/agent_hub/v2/provider_runtime.py`, `src/agent_hub/v2/tools.py`, `README.md`, 버전 문자열 6곳.
- **검증 실행 결과**: 전체 pytest `896 passed, 2 skipped`; `ruff check` 통과; `./scripts/check-sync.sh` 통과; README verify·document_quality 통과; 공개 도구 14개 유지. 갈래별로 되돌려 각자의 테스트가 깨지는 것을 확인했습니다. 설치 후 daemon이 알리는 스키마에 `max_output_tokens`가 없고 예산 인자가 `max_input_tokens`·`max_total_tokens`·`max_leaf_calls`만 남은 것을 확인했습니다. 상한 없이 같은 긴 글 요청을 돌려 `outline_plan`이 **17,213 output token**을 잘림 없이 냈고 `step_output_truncated` 이벤트는 0건이었습니다. live canary 19 passed, doctor 7/7 pass.
- **현재 리스크**: 마지막 확인 run은 `outline_plan`은 성공했지만 `api_facts`(capability `search`)가 `internal_error`로 564ms 만에 실패해 run이 `outcome_unknown`이 됐습니다. **잘림과는 무관한 별개 문제이고 아직 원인을 찾지 않았습니다.** claude search 경로에서 나며 재현 여부부터 봐야 합니다. planner 하한 8192는 근거 있는 값이지만 측정값은 아닙니다. 3.2.0의 `planner_egress_violation` 고침은 실제 호출로 한 번만 확인했습니다.
- **Do-Not-Repeat**: provider가 보낸 신호를 서비스 경계에서 버리지 마세요. 어댑터가 옳게 판단해도 호출자에게 닿지 않으면 없는 것과 같습니다. 사용자의 답변 상한을 내부 기계장치(planner)에 그대로 물리지 마세요. 이벤트에 새 필드를 넣을 때 `_SAFE_EVENT_FIELDS` allowlist를 함께 갱신하세요. 안 그러면 기록은 되고 조용히 버려집니다. provider가 정하는 문자열은 닫힌 집합이나 길이 제한 있는 패턴만 통과시키세요. 공개 스키마에서 인자를 뺄 때 그 인자를 정당하게 쓰는 내부 호출자가 있는지 먼저 확인하세요. 연결 probe가 그랬습니다. 모델에게 알려주는 제약과 런타임이 심판하는 제약을 따로 두지 마세요. 인자 모양 오류를 맨 `ValueError`로 던지지 마세요. 작업 중인 파일을 `git checkout`으로 되돌리지 마세요. 복사본을 만들어 그걸로 되돌리세요.
- **다음 한 걸음**: `capability="search"` step이 claude에서 `internal_error`로 즉시 실패하는지 확인하세요. `./.venv/bin/python -m pytest tests/agent_hub/test_v2_provider_runtime.py -k search -q`로 시작하고, 재현되면 `src/agent_hub/v2/provider_runtime.py`의 search 경로를 읽어 원인을 특정하세요.
<!-- agent-hub:handoff:v1:end -->
