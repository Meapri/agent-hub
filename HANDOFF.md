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
- **원래 목표**: Agent Hub를 "선택적 control plane"으로 재정의하기 전에, 실행 경로에서 모델에게 거짓 근거를 주입하는 코드를 지우고 라우팅 층의 실효성을 판정합니다. 4주 계획의 1주차입니다.
- **현재 단계**: 1주차 네 항목을 모두 끝내고 게이트 A·B 판정을 기록했습니다. `week1/remove-false-evidence` 브랜치에 커밋 2건이 있고 push 전입니다.
- **완료**:
  - write 경로가 만들던 거짓 fact pack을 제거했습니다. provider worker는 project_root를 넘기지 않고 cwd가 빈 샌드박스 디렉터리라, 모든 write step이 "Skills/MCP tools/Install commands: [none detected], manifest complete: True (0/0)"를 권위 있는 근거로 받고 있었습니다. writing.py의 수집 함수 4개와 전용 git 헬퍼, doc_facts.py, gather.py를 지웠고 validate_project_root만 core/repository_facts.py로 이관했습니다. writing.py는 이제 subprocess와 pathlib을 import하지 않습니다.
  - 삭제 전 실패하고 삭제 후 통과하는 경계 테스트 3개를 먼저 작성해 red에서 green 전이를 확인했습니다.
  - store 불변식 12개와 모든 테스트 teardown에서 자동 검사하는 conftest를 추가했습니다. _REQUIRED_SCHEMA_COLUMNS와 PRAGMA 루프를 store 오픈 경로에서 invariants로 옮겨 마이그레이션과 검사기가 어긋날 수 없게 했습니다.
  - 게이트 A를 판정했습니다. 사전 기준은 808b46c가 고친 결함 3건 중 2건 이상 소급 검출이었고 결과는 1건이라 미달입니다. 통과로 기록하지 않았습니다.
  - 게이트 B를 판정했습니다. 정책 routing_mode=auto로 durable run 10건을 돌리고 planner를 번갈아 지정해 두 provider가 각각 5회 실행되게 했는데도 provider 변경은 0건이고 전부 cold_start_preserves_planner였습니다.
  - handoff 스냅샷 0행의 원인이 고장이 아니라 미실행임을 배포된 daemon에서 확인했습니다. apply_update가 snapshot.recorded=true를 반환하고 DB에 1행이 기록됩니다.
- **미완**: 2주차의 실패 분류표 통합과 fault injection 스위트, 3주차의 라우팅 층 삭제, 4주차의 provider MCP 중복층·sdk·workflows 삭제와 claude API 키 lane 실험이 남았습니다. 브랜치를 아직 push하지 않았습니다.
- **변경 파일**: `src/google_antigravity_codex/writing.py`, 삭제된 `src/google_antigravity_codex/doc_facts.py`와 `src/orchestrate_codex/gather.py`, `src/agent_hub/core/repository_facts.py`, `src/agent_hub/core/handoff.py`, 신규 `src/agent_hub/v2/invariants.py`와 `tests/conftest.py`, `src/agent_hub/v2/store.py`, 관련 테스트 4개를 바꿨습니다.
- **검증 실행 결과**: 전체 pytest `678 passed, 2 skipped`; `ruff check`와 `ruff format --check`(177 files); `./scripts/check-sync.sh`; `./scripts/check-hub-plugins.sh`; sdist/wheel build를 통과했습니다. main 대비 12 files changed, 480 insertions, 713 deletions로 순 233줄 감소입니다.
- **현재 리스크**: 게이트 A 미달로 불변식은 실패 경로 안전망이 아니라 상태 오염 조기경보로만 신뢰합니다. 실패 경로 검증은 2주차 fault injection이 담당해야 합니다. 배포된 2.4.1 릴리스에는 1주차 변경이 아직 반영되지 않았습니다.
- **Do-Not-Repeat**: 샌드박스 worker에 project_root를 넘겨 fact pack 수집을 되살리지 마세요. worker는 사용자 프로젝트를 볼 정당한 경로가 없습니다. 불변식 술어를 특정 결함에 맞춰 쓰지 마세요. 그런 술어는 구성상 통과하며 아무것도 증명하지 않습니다. routing_context에서 model을 제거하지 않은 채 auto 게이트의 표본 하한만 낮추지 마세요. 버킷이 provider별로 고립되어 있어 하한을 낮춰도 게이트는 열리지 않습니다.
- **다음 한 걸음**: `src/agent_hub/v2/provider_runtime.py`에 `FAILURE_CLASSES: dict[str, str]` 표를 추가하고 86행의 `"operation_failed"` 기본값을 그 표를 통한 조회로 바꾸세요.
<!-- agent-hub:handoff:v1:end -->
