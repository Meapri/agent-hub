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
- **원래 목표**: 게시된 Agent Hub V2-only `main`을 새 checkout에서 설치하고 격리 daemon의 socket, SQLite, provider manifest와 재시작 영속성을 검증하며 발견한 설치 문제를 근본 수정합니다.
- **현재 단계**: commit `651c739`의 clean clone에서 설치와 daemon restart smoke를 완료했습니다. macOS 기본 Python 3.9 때문에 README 명령이 실패하는 문제를 재현하고 Python 3.10+를 자동 선택하는 bootstrap과 회귀 테스트를 구현했습니다.
- **완료**:
  - 원격 `main`의 `651c73973f0a7afea2ebc675f192b392593017e4`를 임시 새 checkout으로 clone해 게시된 commit을 확인했습니다.
  - macOS `/usr/bin/python3` 3.9.6으로 venv를 만들면 package의 `requires-python >=3.10`에서 설치가 실패함을 재현했습니다.
  - `scripts/bootstrap.sh`를 추가해 `python3.13`부터 `python3.10`, 호환되는 `python3` 순서로 선택하고 `AGENT_HUB_PYTHON` override를 지원합니다. 기존 `.venv`가 3.10 미만이면 삭제하지 않고 안전하게 중단합니다.
  - root, Codex·Claude Code hub, Claude·Grok·Antigravity 개발 README를 bootstrap 명령으로 통일하고 명시적 호환·비호환 interpreter 회귀 테스트를 추가했습니다.
  - clean checkout에서 bootstrap이 Python 3.11.15를 선택해 editable dev install을 완료했고 `agent-hub`, `agent-hubd`, `agent-hub-connect` entrypoint를 확인했습니다.
  - canonical `/private/tmp`의 격리 socket/DB로 daemon을 시작해 공개 도구 14개, Agent Hub 2.0.1, protocol 2.0, Claude/Grok/Gemini/GPT worker manifest protocol 2.0을 확인했습니다.
  - 격리 durable run을 revision 0 queued로 만든 뒤 daemon을 정상 종료·재시작하고 같은 run ID/revision/status와 lease 비활성 상태가 유지되는지 확인했습니다. 재시작 후 DB는 schema 4, WAL, integrity ok였습니다.
  - 검증용 임시 checkout은 daemon 종료 후 macOS 휴지통의 `agent-hub-smoke.9INU4o`로 이동해 복구 가능하게 정리했습니다.
- **미완**: bootstrap 수정 10개 파일은 아직 stage, commit, push하지 않았습니다. clean checkout doctor의 machine-local MCP config check는 setup을 apply하지 않았기 때문에 5 pass/1 fail이었으며 이는 예상된 초기 설치 상태입니다.
- **변경 파일**: `scripts/bootstrap.sh`, `tests/agent_hub/test_bootstrap_script.py`, `README.md`, `hubs/{codex,claude-code}/README.md`, `plugins/{claude-codex,grok-codex,antigravity-codex}/README.md`, `tests/agent_hub/test_readme_copy.py`, `HANDOFF.md`입니다.
- **검증 실행 결과**: bootstrap 집중 회귀 `5 passed`; 전체 pytest `495 passed, 2 skipped`; Ruff; bash syntax; README/HANDOFF document quality; README user-facing verify; Ruler sync; hub plugin check; release version 2.0.1 sync; package build; `git diff --check`가 모두 통과했습니다. clean install, 14-tool daemon, 네 provider manifest, daemon restart durable-run 복구 smoke도 통과했습니다.
- **현재 리스크**: bootstrap은 의도적으로 기존 비호환 `.venv`를 자동 삭제하지 않으므로 사용자가 검토 후 이동하거나 제거해야 합니다. clean checkout의 doctor는 machine-local setup apply 전에는 local_config 실패를 반환합니다. `/tmp`는 macOS에서 symlink이므로 보안 경로 검증을 통과하는 격리 state에는 canonical `/private/tmp`를 사용해야 합니다.
- **Do-Not-Repeat**: macOS의 `python3`가 항상 package 요구 버전을 충족한다고 가정하지 마세요. 기존 `.venv`를 자동 삭제하거나 system Python에 설치하지 마세요. setup 미적용 doctor의 local_config 실패를 daemon·DB 장애로 보고 수리하지 마세요. 격리 state 경로에 `/tmp` symlink spelling을 사용하지 마세요.
- **다음 한 걸음**: `/Users/naen/Git/agent-hub`에서 bootstrap 관련 10개 파일의 diff를 검토한 뒤 별도 commit으로 만들어 `origin/main`에 push하세요.
<!-- agent-hub:handoff:v1:end -->
