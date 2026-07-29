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
- **원래 목표**: 이 저장소가 실제로 어떻게 깨져 왔는지에 근거해 Agent Hub를 개선합니다. 최근 요청은 "출력이 자꾸 끊긴다"에서 시작해, 원인이던 `max_output_tokens` 제거, search 실패 수정, 예산 재산정으로 이어졌습니다.
- **현재 단계**: 3.3.1을 병합(`bf3c177`)하고 설치했습니다. daemon과 bridge 모두 `3.3.1-785f94a0417d`입니다. 진행 중인 작업은 없습니다.
- **완료**:
  - 잘림 신호가 서비스 경계에서 버려지고 있었습니다. 어댑터가 `finish_reason`과 `incomplete_finish_reason:max_tokens`를 만들고 envelope이 실어오는데 `service.py`에 `warnings`라는 단어가 없었습니다. 이제 step checkpoint가 `finish_reason`·`output_truncated`·provider 경고를 담고 `step_output_truncated` 이벤트가 어떤 상한이 잘랐는지 숫자까지 남깁니다. 잘림이 유발한 verifier 실패도 `output_truncated`를 달고 나옵니다. planner는 답변 상한과 무관한 8192 하한을 받습니다.
  - `max_output_tokens`와 별칭 `max_tokens`를 공개 task constraints 스키마에서 뺐습니다. 저장소가 그 값을 시키는 곳은 없었고 호출 에이전트가 스스로 4000·5000·7000을 골랐습니다. 처음에는 정책 경계에서 거절했는데 GUI 연결 probe가 그걸 잡아냈습니다. probe는 `max_tokens=512`로 싼 응답을 원하고 잘려도 상관없으며 그게 per-call 상한의 유일하게 정직한 용도입니다. 런타임은 계속 받고, 없앤 것은 권유뿐입니다.
  - 그 변경이 잠복 결함을 드러냈습니다. 모든 `capability="search"` step이 `internal_error`로 죽었는데 provider는 답을 줬습니다. `HTTP 400: max_tokens: 131072 > 128000`. `chat`은 모델 상한으로 clamp하는데 `search`는 안 했습니다. 또 `search`는 `_call_leaf`로 감싸지 않은 유일한 provider 호출이라 거절이 이름 없이 `internal_error`로 도착했습니다. 둘 다 고쳤고 claude·grok을 감쌌습니다. search canary가 `max_tokens=2048`이라는 스스로 고른 편한 숫자를 써서 운영이 지나지 않는 경로를 시험하고 있었던 것도 고쳤습니다.
  - 예산이 잘못된 양으로 잡혀 있었습니다. `max_output_tokens`와 `max_total_tokens`가 둘 다 131072였는데, 하나는 한 번의 응답을 다른 하나는 계획 전체를 제한합니다. 실측으로 web search 한 단계가 247,203 토큰을 썼으니 **한 단계도 run 예산 안에 안 들어갔습니다.** search가 든 계획은 첫 단계 뒤 항상 멈춰 사람의 예산 부여를 기다렸습니다. `max_total_tokens`를 4,000,000으로 올렸습니다. 계획은 최대 12단계이므로 실측 최대 단계로 가득 찬 계획 하나가 들어가는 크기이고, 소진되면 여전히 `token_budget_grant`로만 이어갈 수 있어 폭주는 사람 앞에서 멈춥니다.
  - 그런데 3.3.0을 깔아도 이 기계에서는 아무것도 안 바뀌었습니다. 정책 파일에 legacy `max_tokens = 131072`만 있었고 `_normalize_policy`가 그 하나를 output과 total 양쪽에 넣어 run 예산을 per-call 값으로 되돌리고 있었습니다. 두 기본값이 같은 숫자일 때만 무해했던 매핑입니다. 이제 `max_tokens`는 `max_output_tokens`만 채우고, run 예산을 원하는 정책은 `max_total_tokens`를 씁니다. 이 저장소의 정책 파일은 직접 편집하지 않고 prepare/apply 경로로 옮겨 두 예산을 명시합니다.
  - 새 테스트가 자격증명 있는 기계에서만 통과하는 것을 CI가 잡았습니다. `run_search`가 API 호출 뒤 `auth.resolve_auth`를 부르는데 API만 stub했습니다. 이제 빈 환경과 없는 홈 디렉터리로도 스위트를 돌려 확인합니다.
- **미완**: provider MCP 프로토콜 계층 2,228줄은 사용자 판단으로 유지합니다. gh 활성 계정 문제는 사용자가 나중에 보기로 했습니다.
- **변경 파일**: 신규 `tests/agent_hub/test_truncated_output_is_visible.py`, `tests/agent_hub/test_search_output_cap.py`, `tests/agent_hub/test_run_budget_default.py`. 수정 `src/agent_hub/v2/service.py`, `store.py`, `provider_runtime.py`, `tools.py`, `policy.py`, `contracts.py`, `src/claude_codex/search.py`, `tests/live/test_capability_canaries.py`, `tests/agent_hub/test_v2_policy_egress.py`, `README.md`, 버전 문자열 6곳.
- **검증 실행 결과**: 전체 pytest `911 passed, 2 skipped`; 자격증명 없는 빈 환경에서도 같은 결과; `ruff check`·`./scripts/check-sync.sh`·README verify·document_quality 통과; 공개 도구 14개 유지. 고침을 갈래별로 되돌려 각자의 테스트가 깨지는 것을 확인했습니다. **설치된 3.3.1에서 search 2단계 + write + review + write 5단계 계획이 예산 부여 없이 `completed`로 끝났습니다.** 500,697 / 4,000,000 토큰, 잘림 이벤트 0건, 일시정지 0건, 최종 결과물 40,092바이트입니다. 같은 요청이 이 세션 내내 첫 단계 뒤 멈추거나 search에서 죽던 것입니다.
- **현재 리스크**: 4,000,000은 실측(최대 단계 247,203 × 최대 12단계)에서 유도했지만 이 기계의 표본은 작습니다(search 3건, chat 5건, write 12건). 표본이 늘면 다시 재는 편이 좋습니다. 예산이 커진 만큼 잘못된 계획이 멈추기 전에 쓰는 양도 커집니다. planner 하한 8192는 근거는 있지만 측정값이 아닙니다. 3.2.0의 `planner_egress_violation` 고침은 실제 호출로 한 번만 확인했습니다.
- **Do-Not-Repeat**: 서로 다른 양을 재는 두 설정에 같은 기본값을 주지 마세요. 같은 숫자인 동안에는 호환 별칭이 둘을 뭉개도 아무도 모릅니다. 같은 규칙을 두 어댑터 경로에 나눠 두지 마세요. `chat`에만 있던 clamp가 `search`에 없어서 깨졌습니다. provider 호출을 `_call_leaf` 없이 두지 마세요. canary가 스스로 편한 숫자를 고르게 두지 마세요. 운영이 쓰는 값을 보내야 합니다. provider stub을 만들 때 인증 경로도 함께 stub하세요. provider가 보낸 신호를 서비스 경계에서 버리지 마세요. 이벤트에 새 필드를 넣을 때 `_SAFE_EVENT_FIELDS`를 함께 갱신하세요. 공개 스키마에서 인자를 뺄 때 그 인자를 정당하게 쓰는 내부 호출자가 있는지 먼저 확인하세요. 작업 중인 파일을 `git checkout`으로 되돌리지 마세요.
- **다음 한 걸음**: 며칠 뒤 `sqlite3 ~/.agent-hub/state.sqlite3 "select capability, count(*), max(total_tokens) from steps where total_tokens > 0 group by 1"`를 실행해 표본이 늘어난 실측으로 `src/agent_hub/v2/policy.py`의 `max_total_tokens` 4,000,000이 여전히 맞는지 확인하세요.
<!-- agent-hub:handoff:v1:end -->
