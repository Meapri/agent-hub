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
- **현재 단계**: release 보상 transaction과 GPT plugin 격리 결함을 수정한 v2.1.3을 설치했습니다. 검증된 변경을 commit/push하는 단계입니다.
- **완료**:
  - `src/agent_hub/v2/release.py::apply_switch`의 bootout→DB restore→candidate bootstrap/health 실패를 하나의 보상 경계로 묶었습니다.
  - candidate를 중지하지 못하면 DB를 건드리지 않으며, 이전 LaunchAgent·DB·daemon health까지 복구돼야 `release_activation_failed`로 판정합니다. 보상이 불완전하면 `release_recovery_failed`와 안전한 실패 단계만 반환합니다.
  - 자동 보상도 실패하면 전환 직전 DB를 rollback slot 옆 emergency snapshot으로 보존하고, 검토되지 않은 다음 switch가 rollback slot이나 emergency snapshot을 덮어쓰기 전에 `release_recovery_pending`으로 차단합니다.
  - DB restore 예외, candidate bootstrap 실패, 이전 daemon 재시작 실패, emergency snapshot 보존·후속 update의 rollback slot 불변성을 failure-injection test로 고정했습니다.
  - GPT provider의 Codex 격리 실행에 `--disable plugins`를 추가했습니다. `--ignore-user-config`만으로는 설치된 plugin skills가 로드되어 skill context budget 오류로 generation이 실패한다는 것을 실제 재현한 뒤 수정했습니다.
  - package·Codex plugin·Claude plugin 버전을 2.1.3으로 동기화하고 immutable runtime `/Users/naen/.agent-hub/releases/2.1.3-4eb3f9fb705e`를 staging·활성화했습니다. host MCP config와 LaunchAgent도 같은 bridge/runtime을 가리킵니다.
  - Codex와 Claude Code plugin을 2.1.3으로 갱신했습니다.
  - Claude `claude-opus-5`, Grok `grok-4.5`, GPT `gpt-5.6-sol`이 설치된 v2.1.3 daemon에서 실제 `AGENT_HUB_OK` generation을 반환했습니다.
- **미완**: Gemini는 `logged_in=true`지만 `auth_ready=false`, `auth_refresh_available` 상태라 실제 generation이 `no_eligible_provider`로 차단됩니다. 연결 GUI에서 세션 갱신이 필요합니다. dependency lock/hash 기반 offline staging, incremental context index, planner validation 진단도 남아 있습니다.
- **변경 파일**: `src/agent_hub/v2/release.py`, `src/openai_codex/client.py`, release/provider 테스트, `README.md`, v2 protocol 문서, package/plugin/version manifest를 변경했습니다.
- **검증 실행 결과**: 전체 pytest `553 passed, 2 skipped`; 전체 Ruff check; release version sync 2.1.3; Ruler sync; README document quality; `git diff --check`를 통과했습니다. installed doctor는 `7 pass, 0 warn, 0 fail`, DB schema 7/WAL/integrity OK입니다. candidate DB-copy health와 LaunchAgent activation도 통과했습니다.
- **현재 리스크**: review run `4ab35fd5d8d03887`은 local inspect artifact `art_29dee50792f4d033e1c459d2`까지 완료했지만 Claude review step이 안전한 `operation_failed`로 종료됐습니다. GPT planner도 `plan_validation_failed`를 반환해 LLM review는 완료 근거로 쓰지 않았고 deterministic test와 실제 canary만 근거로 사용했습니다. Gemini 인증은 현재 호출 불가입니다.
- **Do-Not-Repeat**: connected/logged_in을 generation 성공으로 표현하지 마세요. Codex leaf에서 `--ignore-user-config`가 plugin을 비활성화한다고 가정하지 마세요. `release_recovery_failed` 뒤 emergency snapshot을 삭제하거나 다음 release로 덮어쓰지 마세요. active external step에 중복 continue를 보내지 마세요.
- **다음 한 걸음**: `src/agent_hub/v2/provider_runtime.py::plan`이 최종 `plan_validation_failed` 전에 수집한 validation 오류를 raw plan 없이 안전한 reason taxonomy로 집계하도록 만들고, `tests/agent_hub/test_v2_provider_runtime.py`에 malformed planner 응답의 repair·최종 진단 fixture를 추가하세요.
<!-- agent-hub:handoff:v1:end -->
