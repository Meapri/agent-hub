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
- **원래 목표**: Agent Hub를 V2-only local-first 실행 플랫폼으로 정리하고 Claude, Grok, Gemini, GPT의 공통 provider 계약과 현재 실생성 가능 여부를 검증한 상태로 배포합니다.
- **현재 단계**: V1 제거, V2-only 진입점·문서·플러그인 정리, provider conformance와 오류 비식별화가 완료됐습니다. 사용자가 최종 commit·push를 승인했고 이 HANDOFF를 포함한 검증 완료 changeset을 `main`에 게시합니다.
- **완료**:
  - V1 전용 operations/server/provider adapter, standalone orchestrator runtime·plugin, importer, 오래된 실행 보고서·evidence·테스트를 제거하고 공개 패키지와 plugin entrypoint를 V2-only 계약으로 정리했습니다.
  - `tests/agent_hub/test_v2_provider_conformance.py`로 네 provider의 subprocess status, public catalog, 공통 task invoke, 실패 승격, generation 기록과 tri-state 독립성을 같은 계약으로 검사합니다.
  - `src/agent_hub/v2/service.py`에서 provider 오류의 code와 retryability만 보존하고 내부 메시지는 안전한 고정 문구와 reason code로 비식별화했습니다.
  - Claude, Grok, Gemini, GPT가 모두 callable/live 상태에서 지정된 public model로 pinned live generation canary를 통과했습니다.
  - `origin/main`과 local `main`의 divergence가 0/0임을 확인했고 추가된 줄에서 일반적인 API key, OAuth token, private key 패턴을 검사해 발견 사항이 없었습니다.
- **미완**: 이번 V2-only 배포 범위의 기능 미완 항목은 없습니다. 게시 후 새 checkout과 새 daemon process에서 설치·복구 smoke test를 한 번 수행해야 합니다.
- **변경 파일**: V1 제거와 V2-only 전환을 포함해 staged 기준 111개 파일이 변경됐습니다. 핵심은 `src/agent_hub/v2/`, `src/agent_hub/connect_service.py`, `src/agent_hub/local_setup.py`, `pyproject.toml`, `README.md`, `docs/architecture/agent-hub-v2-protocol.md`, `hubs/`, provider adapter 보강, v2 회귀 테스트, 삭제된 V1 runtime·plugin·test입니다.
- **검증 실행 결과**: 전체 pytest `493 passed, 2 skipped`; Ruff; README와 HANDOFF document quality; Ruler sync; hub plugin check; release version `2.0.1` 동기화; `python -m build`; `git diff --check`와 staged diff check가 모두 통과했습니다. live doctor는 7 pass/0 warn/0 fail이고 SQLite schema 4/WAL/integrity ok이며 네 provider의 live canary가 성공했습니다.
- **현재 리스크**: 이번 changeset은 111개 파일에서 V1 코드와 테스트를 대량 제거하므로 이전 37-tool API 소비자는 호환되지 않습니다. generation verification은 확인 시점 증거이며 OAuth 만료나 외부 provider 장애를 보장하지 않습니다. Claude의 max output clamp 경고는 비치명적이지만 새 모델 한도는 계속 canary로 검증해야 합니다.
- **Do-Not-Repeat**: 삭제된 V1 API나 `plugins/orchestrate-codex`를 호환 경로로 되살리지 마세요. connected, callable auth, live catalog, verified generation을 하나의 boolean으로 합치지 마세요. placeholder model ID와 provider 원문 예외를 공개 계약에 전달하지 마세요.
- **다음 한 걸음**: push 후 임시 새 checkout에서 `agent-hub doctor --project-root .`를 실행해 설치된 V2 daemon socket, SQLite schema, 네 provider worker manifest를 확인하세요.
<!-- agent-hub:handoff:v1:end -->
