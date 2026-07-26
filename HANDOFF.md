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
- **원래 목표**: Claude Code 재검증에서 남은 Agent Hub 2.3.0 결함과 새 회귀를 근본 수정하고, V1 잔재·구조·검증 자동화까지 함께 정리합니다.
- **현재 단계**: `main` HEAD `3bb8ea2` 위의 Agent Hub 2.3.0 hardening working tree는 전체 검증과 재검사를 통과했습니다. 사용자가 commit/push를 승인해 전체 변경과 이 HANDOFF를 하나의 목적 커밋으로 만들기 직전입니다.
- **완료**:
  - provider sandbox의 `parents[3]` fail-open을 제거하고 Python/runtime·HOME 경로를 엄격 검증했습니다. `/`, HOME 조상, symlink 우회, 비ASCII SBPL, cleanup 실패 경로와 macOS 실행 canary를 테스트했습니다.
  - macOS provider sandbox가 요청 runtime뿐 아니라 canonical runtime parent, 즉 실제 `$TMPDIR` 아래의 형제 AF_UNIX socket도 차단하도록 보강했습니다. host 환경변수를 신뢰하지 않고 검증된 runtime 경로에서 parent를 계산하며, 실제 sandbox에서 socket 차단과 worker 정상 기동을 함께 확인했습니다.
  - 연결 UI 세션 비교를 ASCII fail-closed helper로 통일했습니다. 비ASCII query/header와 비ASCII server token은 연결을 끊는 `TypeError` 대신 JSON 401 `session_required`를 반환하고, query 없는 공개 `/` bootstrap 화면은 계속 200으로 제공합니다.
  - dependency artifact를 `DependencyContextPart`로 구분하고 같은 run의 완료된 local `inspect` step이 만든 JSON artifact만 trusted fact pack으로 병합합니다. provider가 위조한 `fact_pack_v2`는 plain text로 보존합니다.
  - `max_input_tokens`, `max_output_tokens`, `max_total_tokens`를 분리했습니다. legacy `max_tokens`는 output·total 호환 alias로만 이동하고 provider에는 output 한도만 전달합니다.
  - cancel을 store CAS 우선으로 바꾸고 queued/running step을 원자 취소합니다. wave 예외 후 claim-fenced reconciliation이 retry-safe local step은 queued, 전송 결과가 불명확한 외부 step은 `outcome_unknown`으로 정산하며 늦은 결과를 연결하지 않습니다.
  - 실패 run에 `retryable_failed_steps`와 revision이 포함된 `next_action`을 노출하고 tool schema, packaged `run_v3`, README, protocol, adaptive/takeover skill과 양쪽 host 생성물을 동기화했습니다.
  - `get`, `events`, `cancel`, `feedback`, `policy`, `doctor` 공개 dispatch 회귀 테스트를 추가했습니다.
  - `connect_service.py`의 장시간 status/RPC를 전역 RLock 밖으로 옮기고 시작 reservation으로 경합을 막았습니다. egress daemon 통신, status projection, 공유 타입을 `connect_egress.py`, `connect_status.py`, `connect_types.py`로 분리해 공개 import 호환을 유지했습니다.
  - 죽은 V1 경로인 `core/mcp.py`, `core/rpc.py`, `core/parallel.py`, legacy `orchestrator.execute_plan`/call-budget 계층, 사용되지 않던 gather git/code-context 약 1,000줄, 제거된 Gemini `agy-cli` transport와 자체 테스트를 삭제했습니다. durable facts와 project-root 검증은 보존했고 삭제 모듈 부재 테스트를 추가했습니다.
  - archived Antigravity plugin 문서·manifest·skill에서 제거된 `agy-cli` 지원 주장을 없애고 direct OAuth/`agy-oauth` 경계와 맞췄습니다.
  - macOS CI를 Python 3.10·3.12 matrix로 추가해 pytest, Ruff lint/format, Ruler/plugin sync, version check, build를 실행하도록 했습니다. 전체 `src`·`tests`를 Ruff format 기준선에 맞췄습니다.
- **미완**: 로컬 설치본과 실행 중인 daemon에는 이번 source 변경을 적용하지 않았습니다. 새 CI workflow는 push 이후 처음 GitHub Actions에서 실행됩니다. OAuth job·model catalog 상태는 한 원자적 상태 기계라 이번에는 부분 분리하지 않았습니다. 기존 멈춘 run 5개는 사용자 승인 없이 재시도하지 않았습니다.
- **변경 파일**: 핵심은 `src/agent_hub/v2/{provider_client.py,dependency_context.py,contracts.py,provider_worker.py,policy.py,service.py,store.py,tools.py}`, 연결 UI 서비스는 `src/agent_hub/connect_{app,service,egress,status,types}.py`, V1 정리는 `src/agent_hub/orchestrator.py`, `src/orchestrate_codex/gather.py`, 삭제된 core/Google CLI 모듈입니다. `.github/workflows/ci.yml`, README·protocol·skills·Antigravity snapshot 문서와 관련 tests를 함께 변경했습니다.
- **검증 실행 결과**: 전체 pytest `629 passed, 2 skipped in 18.46s`; 이번 두 결함 집중 테스트 `56 passed`; `ruff check src tests`; `ruff format --check src tests`(173 files); `node --check src/agent_hub/connect_ui/app.js`; `git diff --check`; `./scripts/check-sync.sh`; `./scripts/check-hub-plugins.sh`; release version 2.3.0 정합성; package sdist/wheel build를 통과했습니다. README와 V2 protocol user-facing verify 및 document quality도 통과했습니다.
- **현재 리스크**: 사용자가 전역 자동 승인을 직접 켜 현재 daemon 설정은 `auto_approve=true`, revision 1입니다. 이는 정책상 허용된 모든 프로젝트 egress에서 개별 검토를 생략하므로 의도된 편의 기능이지만 범위가 큽니다. 소스는 설치본보다 앞서 있으므로 재시작만으로 이번 수정이 적용되지는 않습니다. `connect_service.py`는 1,720줄로 줄었지만 OAuth lifecycle을 안전하게 더 분리하려면 공유 state와 generation CAS를 먼저 설계해야 합니다.
- **Do-Not-Repeat**: broad runtime path를 file-read allowlist에 넣지 마세요. runtime parent의 AF_UNIX deny를 제거하거나 환경의 `$TMPDIR` 문자열을 검증 없이 SBPL에 넣지 마세요. 비ASCII 값을 `secrets.compare_digest`에 직접 전달하지 마세요. provider JSON을 provenance 확인 없이 fact pack으로 승격하지 마세요. `max_tokens`를 입력 한도로 재사용하지 마세요. `outcome_unknown`이나 취소된 외부 호출을 자동 재전송하지 마세요. 사용자 전역 자동 승인 값과 멈춘 run을 에이전트 판단으로 바꾸지 마세요. 제거한 V1/`agy-cli` 모듈을 compatibility 명목으로 되살리지 마세요.
- **다음 한 걸음**: push된 2.3.0 hardening 커밋의 GitHub Actions `ci.yml` 결과를 확인한 뒤, 사용자가 설치 적용을 요청할 때만 로컬 update preview를 생성하세요.
<!-- agent-hub:handoff:v1:end -->
