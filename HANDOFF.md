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
- **원래 목표**: 이 저장소가 실제로 어떻게 깨져 왔는지에 근거해 Agent Hub를 개선합니다. 최근 요청은 "출력이 자꾸 끊긴다"였고, 이어서 원인이던 `max_output_tokens`를 빼는 일, 그 뒤 드러난 search 실패를 잡는 일이었습니다.
- **현재 단계**: 3.2.3을 병합(`5a14f72`)하고 설치했습니다. daemon과 bridge 모두 `3.2.3-f9be1fb8e06d`입니다. 진행 중인 작업은 없습니다.
- **완료**:
  - 출력 끊김의 원인은 `max_output_tokens`였고 세 군데가 그것을 감췄습니다. 잘림 신호를 service가 버리고 있었고(`service.py`에 `warnings`라는 단어가 없었습니다), 사용자의 답변 상한이 planner까지 목 졸랐으며, 잘림이 유발한 verifier 실패가 잘림을 가리키지 않았습니다. 이제 step checkpoint가 `finish_reason`·`output_truncated`·provider 경고를 담고 `step_output_truncated` 이벤트가 어떤 상한이 잘랐는지 숫자까지 남깁니다. planner는 8192 하한을 받습니다.
  - `max_output_tokens`와 별칭 `max_tokens`를 공개 task constraints 스키마에서 뺐습니다. 저장소가 그 값을 설정하라고 시키는 곳은 없었고 호출하는 에이전트가 스스로 4000·5000·7000을 골랐습니다. 설명 없는 정수 knob이 부르는 추측입니다. 처음에는 정책 경계에서 아예 거절했는데 **GUI 연결 probe가 그걸 잡아냈습니다.** probe는 `max_tokens=512`로 싼 응답을 원하고 잘려도 상관없으며 그게 per-call 상한의 유일하게 정직한 용도입니다. 런타임은 계속 받고, 없앤 것은 권유뿐입니다.
  - 그 변경이 잠복해 있던 결함을 드러냈습니다. 모든 `capability="search"` step이 0.5초 만에 `internal_error`로 죽었는데 provider는 실제로 답을 줬습니다. `HTTP 400: max_tokens: 131072 > 128000`. `chat`은 모델 상한으로 clamp하는데 `search`는 안 했고, 호출자가 4000·7000을 주는 동안은 그 틈이 드러날 수 없었습니다. 정책 기본값 131072가 그대로 흐르자 전부 깨졌습니다. 이제 `search`도 `chat`과 똑같이 clamp하고 clamp했다고 경고합니다.
  - `search`는 `_call_leaf`로 감싸지 않은 유일한 provider 호출이었습니다. 그래서 거절이 맨 `RuntimeError`로 올라와 step 기록에 `internal_error`로 남았고, provider도 이유도 이름이 없었습니다. 진단에 실제 run이 필요했던 이유입니다. claude와 grok을 감쌌고 gemini는 이미 감싸져 있었습니다.
  - search canary가 `max_tokens=2048`이라는 스스로 고른 편한 숫자를 쓰고 있어서 운영이 지나지 않는 경로를 시험하고 있었습니다. 이제 프로젝트 기본값을 보내고, 두 고침이 없으면 실패합니다.
  - 새 테스트가 자격증명 있는 기계에서만 통과하는 것을 CI가 잡았습니다. `run_search`가 API 호출 뒤 `auth.resolve_auth`를 부르는데 API만 stub했습니다. 빈 환경과 없는 홈 디렉터리로 스위트를 돌려 확인했습니다.
- **미완**: provider MCP 프로토콜 계층 2,228줄은 사용자 판단으로 유지합니다. gh 활성 계정 문제는 사용자가 나중에 보기로 했습니다.
- **변경 파일**: 신규 `tests/agent_hub/test_truncated_output_is_visible.py`, `tests/agent_hub/test_search_output_cap.py`. 수정 `src/agent_hub/v2/service.py`, `src/agent_hub/v2/store.py`, `src/agent_hub/v2/provider_runtime.py`, `src/agent_hub/v2/tools.py`, `src/claude_codex/search.py`, `tests/live/test_capability_canaries.py`, `README.md`, 버전 문자열 6곳.
- **검증 실행 결과**: 전체 pytest `903 passed, 2 skipped`; 자격증명 없는 빈 환경에서도 같은 결과; `ruff check` 통과; `./scripts/check-sync.sh` 통과; README verify·document_quality 통과; 공개 도구 14개 유지. 고침을 갈래별로 되돌려 각자의 테스트가 깨지는 것을 확인했습니다(clamp 2건, wrapper 3건, 잘림 가시성 여러 건). 실제 provider로 HTTP 400을 내던 같은 호출이 출처 7건과 clamp 경고를 달고 성공했습니다. 설치된 3.2.3에서 search 두 단계가 포함된 계획을 돌려 둘 다 completed(출력 7816·8056 토큰)로 끝나는 것을 확인했습니다.
- **현재 리스크**: 그 확인 run은 `run_token_budget_exhausted`로 멈췄습니다. 버그가 아니라 설계대로지만 **기본 예산이 search에 비해 작습니다.** web search 한 단계가 입력 239,387 토큰, 다른 하나가 167,768 토큰을 썼고 정책 기본 `max_total_tokens`는 131,072입니다. search가 들어간 계획은 사실상 항상 예산 부여가 필요합니다. 기본값을 올릴지, search 결과를 잘라 넣을지는 판단이 필요합니다. planner 하한 8192는 근거는 있지만 측정값이 아닙니다. 3.2.0의 `planner_egress_violation` 고침은 실제 호출로 한 번만 확인했습니다.
- **Do-Not-Repeat**: 같은 규칙을 두 어댑터 경로에 나눠 두지 마세요. `chat`에만 있던 clamp가 `search`에 없어서 정확히 그렇게 깨졌습니다. provider 호출을 `_call_leaf` 없이 두지 마세요. 실패가 이름 없이 `internal_error`로 도착합니다. canary가 스스로 편한 숫자를 고르게 두지 마세요. 운영이 쓰는 값을 보내야 합니다. 테스트에서 provider stub을 만들 때 인증 경로도 함께 stub하세요. 안 그러면 로그인된 기계에서만 통과합니다. provider가 보낸 신호를 서비스 경계에서 버리지 마세요. 사용자의 답변 상한을 planner에 물리지 마세요. 이벤트에 새 필드를 넣을 때 `_SAFE_EVENT_FIELDS`를 함께 갱신하세요. 공개 스키마에서 인자를 뺄 때 그 인자를 정당하게 쓰는 내부 호출자가 있는지 먼저 확인하세요. 작업 중인 파일을 `git checkout`으로 되돌리지 마세요.
- **다음 한 걸음**: `sqlite3 ~/.agent-hub/state.sqlite3 "select step_id, capability, input_tokens, total_tokens from steps where capability='search' and total_tokens > 0"`를 실행해 search 단계의 실제 소비량을 뽑고, 그 값을 근거로 `src/agent_hub/v2/policy.py`의 기본 `max_total_tokens` 131072를 올릴지 사용자와 정하세요.
<!-- agent-hub:handoff:v1:end -->
