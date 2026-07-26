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
- **원래 목표**: Agent Hub v2를 실제 사용 가능한 local-first multi-provider runtime으로 발전시키고, 구현·검증·설치·Git 반영까지 완료합니다.
- **현재 단계**: 반복되던 Gemini access token 만료가 실제 세션 단절처럼 라우팅을 막던 원인을 수정한 v2.1.4를 설치했고, 검증된 변경을 commit/push하는 단계입니다.
- **완료**:
  - Gemini의 access token이 만료돼도 refresh token과 사용 동의가 유효하면 `provider_runtime.status`가 read-only 상태인 `ready=false`, `refreshable=true`를 유지하면서 실행 가능성만 `invocation_ready=true`, `auto_refresh_on_invoke=true`로 분리해 보고하도록 했습니다.
  - `HubService` 라우터가 `invocation_ready`를 실행 eligibility에 사용하게 해 provider worker가 실제 요청 직전에 기존 `valid_credentials(refresh=True)` 경로로 토큰을 자동 갱신할 수 있게 했습니다. status의 `ready` 의미는 바꾸지 않았고 invoke·catalog 뒤 status cache를 무효화하거나 갱신합니다.
  - 연결 GUI가 자동 갱신 가능한 Gemini를 세션 단절로 표현하지 않고, 실제 연결 테스트와 live model catalog 조회가 수동 갱신 버튼 없이 자동 갱신 경로를 사용하도록 바꿨습니다.
  - 사용 동의가 없는 refreshable 계정은 `invocation_ready=false`로 유지하는 provider contract test, 만료 상태를 캐시한 뒤에도 Gemini invoke를 허용하고 갱신 후 ready 상태를 다시 읽는 service test, GUI/connection manager 회귀 테스트를 추가했습니다.
  - package·Codex plugin·Claude plugin 버전을 2.1.4로 동기화하고 immutable runtime `/Users/naen/.agent-hub/releases/2.1.4-d3a6e3a58b81`을 staging·활성화했습니다. Codex와 Claude Code plugin도 2.1.4로 갱신했습니다.
  - 설치된 v2.1.4 daemon에서 Gemini `gemini-3.6-flash-high`가 실제 `AGENT_HUB_GEMINI_OK` generation을 반환했습니다.
- **미완**: refresh token 자체가 폐기되거나 계정 권한이 회수된 경우에는 보안상 자동 복구하지 않고 GUI 재로그인이 필요합니다. dependency lock/hash 기반 offline staging, incremental context index, planner validation 진단도 남아 있습니다.
- **변경 파일**: `src/agent_hub/v2/provider_runtime.py`, `src/agent_hub/v2/service.py`, `src/agent_hub/connect_service.py`, `src/agent_hub/connect_ui/app.js`, 관련 provider/service/GUI 테스트, `README.md`, package/plugin version manifest를 변경했습니다.
- **검증 실행 결과**: 전체 pytest `558 passed, 2 skipped`; 전체 Ruff check; release version sync 2.1.4; Ruler sync; Hub plugin sync; README `user_facing=true` verify와 document quality; JavaScript syntax; `git diff --check`를 통과했습니다. 설치된 doctor는 `6 pass, 0 warn, 0 fail`, DB schema 7/WAL/integrity OK이며 LaunchAgent는 v2.1.4 daemon으로 실행 중입니다. 실제 Gemini generation canary도 통과했습니다.
- **현재 리스크**: 만료 access token 경로는 실제 credential을 강제로 변조하지 않고 회귀 fixture로 검증했습니다. live canary 시점에는 자동 갱신 뒤 token이 이미 유효했으므로, 다음 자연 만료 때 자동 갱신 관측을 한 번 더 확인할 가치가 있습니다.
- **Do-Not-Repeat**: read-only status 조회에서 토큰을 갱신하지 마세요. `logged_in`, `refreshable`, `ready`, `invocation_ready`, 실제 generation 성공을 같은 상태로 표현하지 마세요. 다른 provider까지 `refreshable`만으로 일반화하지 말고 worker의 자동 갱신 계약을 먼저 증명하세요. active external step에 중복 continue를 보내지 마세요.
- **다음 한 걸음**: `src/agent_hub/v2/provider_runtime.py::plan`이 최종 `plan_validation_failed` 전에 수집한 validation 오류를 raw plan 없이 안전한 reason taxonomy로 집계하고 `tests/agent_hub/test_v2_provider_runtime.py`에 malformed planner 응답의 repair·최종 진단 fixture를 추가하세요.
<!-- agent-hub:handoff:v1:end -->
