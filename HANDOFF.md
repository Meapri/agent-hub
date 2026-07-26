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
- **원래 목표**: Agent Hub의 README 작성 과정과 Claude Code 저장소 감사를 현재 checkout에 대조하고, 아직 남아 있으며 타당한 실행·보안·복구 문제를 근본 원인 기준으로 수정한 뒤 커밋·푸시합니다.
- **현재 단계**: `main`의 기준 커밋 `6cae011` 위에서 감사 후속 수정과 독립적인 GUI egress 승인, provider sandbox 강화를 구현했습니다. 패키지·플러그인 버전은 `2.2.0`, 저장소 schema는 8이며, 전체 검증을 마쳐 이 HANDOFF와 함께 단일 개선 커밋으로 묶을 준비가 됐습니다.
- **완료**:
  - retry-safe failed step의 명시적 CAS 재대기열화, 병렬 wave 예외 격리와 완료 checkpoint 보존, `outcome_unknown` 재호출 방지를 구현했습니다.
  - repair backup의 schema/integrity 재검증과 활성 daemon DB 교체 차단, update 실패 시 기존 rollback metadata·plist·DB snapshot 보존을 구현했습니다.
  - source 민감 경로 거부와 credential·PEM redaction을 확대하고, provider별 홈 읽기 경계를 제한했습니다.
  - provider worker sandbox가 외부 TCP/UDP뿐 아니라 직접 DNS와 사용자 홈·runtime·임시 디렉터리의 Unix socket을 차단하면서 Python worker와 localhost egress proxy는 계속 동작하도록 했습니다.
  - repository/artifact egress에 15분 TTL의 `egress_review_v1`을 추가했습니다. proposal·manifest·policy revision·project root에 결합된 승인을 연결 GUI에서만 승인/거부하고 `apply`가 한 번 원자적으로 소비합니다. 승인 전과 재사용 시 provider 호출은 0회입니다.
  - egress review 조회·결정은 daemon의 내부 GUI 채널에만 두고 14개 공개 MCP 도구에는 승인 mutation을 추가하지 않았습니다. GUI 요청에는 세션 인증, same-origin, visible-intent 검사를 적용했습니다.
  - planner capability 계약, safe handoff 오류, dependency context 압축, live model input-limit cache, provider fallback model과 Gemini session 복구 경로를 정비했습니다.
  - artifact AES-GCM 변조/AAD/digest, macOS Keychain mock, release rollback, repair, provider sandbox, GUI approval을 포함한 회귀 테스트를 추가했습니다.
  - README, NOTICE, V2 protocol, 공유 skill 정본과 Codex/Claude 생성물을 실제 동작에 맞게 동기화하고 버전을 `2.2.0`으로 올렸습니다.
- **미완**: legacy V1 잔여 모듈 제거와 공개 도구 전체 dispatch coverage는 별도 작업입니다. 저장소 전체 `ruff format --check`에는 이번 변경과 무관한 기존 63개 파일 차이가 남아 있습니다. 현재 로컬에 설치되어 실행 중인 Agent Hub는 아직 이전 `2.1.4` 배포본입니다.
- **변경 파일**: 실행·저장소는 `src/agent_hub/v2/service.py`, `store.py`, `repair.py`, `release.py`, `dependency_context.py`; 보안은 `egress.py`, `provider_client.py`; provider/routing은 `provider_manifests.py`, `provider_runtime.py`, `routing.py`, 각 provider adapter; GUI는 `connect_app.py`, `connect_service.py`, `connect_ui/*`; 계약·문서는 `contracts.py`, `tools.py`, `schemas/contracts.json`, `README.md`, `NOTICE.md`, `docs/architecture/agent-hub-v2-protocol.md`, Ruler 정본과 생성물이며 관련 테스트를 함께 변경했습니다.
- **검증 실행 결과**: 전체 pytest `611 passed, 2 skipped in 15.87s`; `ruff check src tests`; 변경 파일 `ruff format`; `node --check src/agent_hub/connect_ui/app.js`; `git diff --check`; Ruler와 Hub plugin sync/check; README·NOTICE·V2 protocol user-facing verify; README·V2 protocol document quality; package version 정합성; `agent_hub-2.2.0` sdist/wheel build를 통과했습니다. 실제 macOS sandbox에서 worker status 성공, 직접 DNS 실패, `/tmp` AF_UNIX 연결 실패를 확인했습니다. 저장소 전체 format check만 기존 63개 파일 때문에 실패합니다.
- **현재 리스크**: 같은 macOS 사용자 권한으로 임의 로컬 코드를 실행할 수 있는 공격자는 private daemon socket에도 접근할 수 있어 GUI 승인은 MCP prompt injection 경계이지 손상된 OS 계정의 보안 경계는 아닙니다. macOS system runtime socket은 worker 호환성을 위해 허용합니다. 일회용 승인은 planner 호출 직전에 소비되므로 호출 실패 시 새 prepare·승인이 필요합니다. live model limit cache는 daemon memory에만 있어 재시작 시 manifest fallback으로 돌아갑니다. 로컬 설치본은 별도 update 전까지 `2.1.4`입니다.
- **Do-Not-Repeat**: GUI 승인 mutation을 공개 MCP 도구로 노출하거나 자동 승인하지 마세요. 소비된 review ID를 재사용하지 마세요. `outcome_unknown`을 자동/명시 재호출하지 마세요. 살아 있는 daemon 아래에서 SQLite 파일을 교체하지 마세요. provider 기동 검증 없이 전체 network/AF_UNIX를 blanket deny하거나 홈 전체 읽기를 다시 허용하지 마세요. 기존 63개 파일을 이 변경과 섞어 repo-wide format하지 마세요.
- **다음 한 걸음**: 푸시된 `2.2.0` commit을 기준으로 `agent-hub update`의 preview/apply 절차를 실행한 뒤 `agent_hub_status`에서 daemon 버전과 schema 8을 확인하고 연결 GUI에서 repository egress prepare→승인→일회용 apply 흐름을 실제 설치본으로 검증하세요.
<!-- agent-hub:handoff:v1:end -->
