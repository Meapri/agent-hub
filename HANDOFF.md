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
- **원래 목표**: 하네스 안에서 여러 provider 모델을 따로 쓰기도, 오케스트레이션하기도, 컨텍스트를 공유하며 스위칭하기도 하는 개인용 실행 환경을 만듭니다. 마지막 요청은 "로그인이 자꾸 풀리는 문제를 완벽히 해결"이었습니다.
- **현재 단계**: 3.4.1을 병합(`57cfb41`)하고 설치했습니다. daemon과 bridge 모두 `3.4.1-16d044ceeb0e`이고 네 provider 중 claude·grok·gemini가 ready입니다. gpt는 Codex 로그인이 풀려 있고 그건 사용자가 `codex login`을 해야 합니다. 진행 중인 작업은 없습니다.
- **완료**:
  - 세 목표를 실제 호출로 확인했습니다. 단발 호출 성공, search 2 + write + review + write 5단계 run `completed`, 그리고 claude가 만든 고유 표식을 gemini가 인용하고 claude가 검증하는 3단계 교차 run `completed`입니다. 컨텍스트 공유 스위칭은 **run 안에서만** 됩니다. run 밖에서 이전 결과를 다른 모델에 넘기려면 `input_artifacts`가 `egress_approval_required`를 던져 plan 경로로 밀어냅니다.
  - 로그인 문제의 원인은 갱신에 시계가 없다는 것이었습니다. 갱신은 만료 직전 좁은 구간에 호출이 들어올 때만 부수효과로 일어났고 `daemon.py`에는 lease recovery 루프 하나뿐이었습니다. 만료되면 `ready: false` → 라우팅이 제외 → 안 불림 → 안 갱신 → 계속 제외. **아프면 안 쓰고 안 쓰니 낫지 못하는 고리**입니다. 새 worker 하나를 띄우자 grok이 갱신되고 전부 green이 된 것이 결정적 증거였습니다.
  - `renew_auth`를 단일 진입점으로 만들었습니다. 두 호출자(daemon 루프, invoke 경로)가 같은 함수를 씁니다. 소유하지 않은 자격증명은 이름 있는 거절을 내고 provider의 말을 그대로 옮기지 않습니다.
  - daemon이 10분마다 갱신합니다. Agent Hub가 소유한 것 중 가장 짧은 gemini 1시간보다 짧아 두 회차 사이에 만료될 수 없습니다. 남의 세션(Claude Code, Codex)은 건드리지 않습니다.
  - `invoke`가 `invocation_ready` 약속을 지킵니다. 라우팅이 갱신 가능한 provider를 통과시키는데 아무도 갱신하지 않아 만료된 토큰으로 호출이 나가고 맨 `RuntimeError`로 돌아왔습니다.
  - 남의 자격증명이 로그아웃이면 주인과 명령을 말합니다. 설치본에서 `gpt is signed out -- run: codex login`을 확인했습니다.
- **미완**: provider MCP 프로토콜 계층 2,228줄은 사용자 판단으로 유지합니다. gh 활성 계정 문제는 사용자가 나중에 보기로 했습니다. planner를 gpt로 두면 교차 provider 계획이 2/2 실패하고 claude로 두면 성공했는데, 원인을 찾지 못했습니다. `plan_validation_failed`만 나오고 실제 검증 메시지는 버려집니다.
- **변경 파일**: 신규 `tests/agent_hub/test_credential_renewal.py`. 수정 `src/agent_hub/v2/provider_runtime.py`, `service.py`, `daemon.py`, `provider_selection.py`, `provider_worker.py`, `tests/agent_hub/test_v2_provider_selection.py`, `README.md`, 버전 문자열 6곳.
- **검증 실행 결과**: 전체 pytest `935 passed, 2 skipped`; 자격증명 없는 빈 환경에서도 같은 결과; `ruff check`·`./scripts/check-sync.sh`·README verify·document_quality 통과; 공개 도구 14개 유지. 갈래별로 되돌려 각자의 테스트가 깨지는 것을 확인했습니다. 설치본 daemon을 띄워 `agent-hub-v2-credential-renewal` 스레드가 실제로 뜨고 갱신 1회가 `{'grok': 'not_due', 'gemini': 'not_due'}`를 돌려주며 `close()` 뒤 스레드가 남지 않는 것을 확인했습니다. 갱신 루프가 돈 뒤 grok이 `ready: false`에서 `ready: true`로 바뀌었습니다.
- **현재 리스크**: `ensure_usable_auth`를 `invoke`에서 빼는 되돌림이 제가 쓴 모든 테스트를 통과했습니다. 규칙은 있는데 연결됐는지 아무도 안 보는, 이 저장소가 반복하는 결함입니다. 배선 테스트를 추가했지만 같은 종류가 다른 곳에 더 있을 수 있습니다. 갱신 주기 600초는 gemini 1시간을 기준으로 잡은 값이지 측정값이 아닙니다. 갱신이 실패했을 때 사용자에게 알리는 경로가 없어 조용히 재시도만 합니다.
- **Do-Not-Repeat**: 상태를 고치는 유일한 방법이 "쓰이는 것"이 되게 두지 마세요. 안 쓰이면 못 낫는 고리가 생깁니다. 남의 프로그램이 관리하는 세션을 대신 갱신하지 마세요. 규칙을 만들고 그것을 부르는 지점을 테스트하지 않으면, 그 한 줄을 지워도 아무 테스트가 안 깨집니다. 같은 규칙을 두 곳에 복제하지 마세요. provider가 보낸 신호를 서비스 경계에서 버리지 마세요. 이벤트에 새 필드를 넣을 때 `_SAFE_EVENT_FIELDS`를 함께 갱신하세요. 공개 스키마에서 인자를 뺄 때 그것을 정당하게 쓰는 내부 호출자가 있는지 먼저 확인하세요. 테스트에서 provider stub을 만들 때 인증 경로도 함께 stub하세요. 작업 중인 파일을 `git checkout`으로 되돌리지 마세요. 병합 뒤에는 `git fetch --prune`을 붙이세요.
- **다음 한 걸음**: 하루 뒤 `sqlite3 ~/.agent-hub/state.sqlite3 "select provider, success, count(*) from provider_health group by 1,2"`와 `agent_hub_status --probe` 결과를 확인해, grok·gemini가 한 번도 `ready: false`로 떨어지지 않았는지 판정하세요.
<!-- agent-hub:handoff:v1:end -->
