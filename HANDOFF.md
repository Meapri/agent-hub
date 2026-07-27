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
- **원래 목표**: 라우팅 학습 층을 걸어내 선택을 결정론적으로 만들고, 오케스트레이션과 별개로 단일 모델 호출과 비전(이미지 입력)을 1급 경로로 지원합니다. 4주 계획의 3주차입니다.
- **현재 단계**: 3주차 세 항목(라우팅 수술, 단일 호출 경로, 비전 입력)을 모두 끝냈습니다. `week3/routing-surgery` 브랜치에 커밋 4건(`d2c1264`, `0a50bbe`, `1006b05`, `19eddf0`)이 있고 push 전입니다.
- **완료**:
  - 조사 결과 3주차 계획을 수정했습니다. 라우팅 층을 통째로 지우면 라우팅이 아닌 기능 9개가 같이 깨집니다(provider 자격 필터링, fallback 순서, context limit 강제, model 결정, wave 토큰 예측, auto 리플랜 게이트, egress·model 정책 검증, plan/policy 계약 필드, 불변식과 수리 액션). 게이트 B가 무너뜨린 것은 학습·점수 계산이지 선택 로직이 아니므로 범위를 좁혔습니다.
  - `capability_token_estimate`를 `routing_samples`에서 떼어내 step 원장을 읽도록 바꿨습니다. 완료된 step이 이미 provider가 청구한 수치를 가지고 있고, 그게 예산 게이트가 차감하는 바로 그 숫자입니다. 커버링 인덱스도 같이 옮겼습니다.
  - `src/agent_hub/v2/provider_selection.py`의 `select_provider()`로 교체했습니다. 인자만의 순수 함수라 같은 입력은 항상 같은 답을 주고, 실패한 run을 입력만으로 설명할 수 있습니다. 자격 있는 provider를 호출자의 allowlist 순서로 시도하고, pinned provider가 불가하면 조용히 대체하지 않고 오류를 냅니다.
  - 자기 채점 폐쇄 루프를 끊었습니다. 결정론적 검증기를 통과한 step이 자기 품질을 1.0으로 기록하고 그 숫자가 다음 provider 선택 순위에 들어가고 있었습니다.
  - routing_profile, routing_prior 정책 타깃, doctor의 routing_prior 블록과 수리 액션, feedback의 라우팅 샘플 기록을 지웠습니다. 스키마 11에서 테이블 3개를 DROP합니다. 순 2,129줄 감소입니다.
  - 비전 입력을 지원합니다. provider 어댑터 4개는 이미 이미지 입력 코드가 있었고 manifest도 vision을 광고했지만, task 계약에 이미지 필드가 없어 vision은 worker에서 항상 거부됐습니다. `input_images` 필드를 추가하고, 샌드박스 밖의 daemon이 파일을 읽어 base64로 바꿔 내려보냅니다. worker는 경로를 보지 않으며 테스트가 그것을 검증합니다.
  - 이미지는 `inline_input`이 아닌 별도 필드로 나릅니다. inline_input은 프롬프트 텍스트라 모델 입력 토큰 창에 계산되고, 그리로 보내면 약 384KiB에서 막힙니다. 별도 필드에서는 worker stdin 상한이 진짜 제약이고, `MAX_TASK_IMAGE_CHARS`를 그 상한에서 유도했습니다(원본 약 2.2MB).
  - durable run이 step task를 다시 만들 때 이미지를 물려주지 않아 계획된 vision step이 invalid_request로 죽는 결함을 테스트가 잡아 고쳤습니다.
  - `agent_hub_execute`와 `agent_hub_plan`의 `task` 인자가 `{"type": "object"}`로만 노출돼 있던 것을 실제 스키마로 바꿨습니다. MCP 호출자는 도구 스키마만 보므로 capability 이름도 input_images 존재도 알 수 없었습니다. 발행 스키마와 validator가 갈라지지 않는지 검사하는 테스트 3건도 넣었습니다.
  - execute의 비밀값 redaction 누락 의심을 검증해 결함이 아님을 확인했습니다. redaction은 `inline_consent` artifact를 의도적으로 제외하며, 호출자가 직접 쓴 프롬프트는 durable 경로에서도 건드리지 않습니다. 두 경로가 일치합니다.
- **미완**: 4주차의 provider MCP 중복층·sdk·workflows 삭제와 claude API 키 lane 실험이 남았습니다. 브랜치를 아직 push하지 않았습니다. 이미지 생성(capability=image)은 여전히 provider 캐시 디렉터리의 파일 경로 문자열만 artifact에 text로 저장하고 실제 바이트는 저장소에 들어오지 않습니다. artifacts.content는 BLOB이라 바이너리를 담을 수 있지만 `_artifact_text`가 UTF-8 디코딩을 강제해 모든 소비 경로가 텍스트 전용입니다.
- **변경 파일**: 신규 `src/agent_hub/v2/provider_selection.py`, `tests/agent_hub/test_v2_provider_selection.py`, `tests/agent_hub/test_v2_vision.py`. 삭제 `src/agent_hub/v2/routing.py`, `src/agent_hub/v2/routing_prior.py`, `tests/agent_hub/test_v2_routing.py`, `tests/agent_hub/test_v2_routing_prior.py`. 수정 `src/agent_hub/v2/service.py`, `store.py`, `contracts.py`, `policy.py`, `tools.py`, `repair.py`, `invariants.py`, `provider_worker.py`, `provider_runtime.py`, `__init__.py`, `schemas/contracts.json`.
- **검증 실행 결과**: 전체 pytest `738 passed, 2 skipped`; `ruff check` 통과; `ruff format --check` 통과; `./scripts/check-sync.sh` 통과; 공개 도구 14개 불변식 유지(`len(TOOL_NAMES)==14`, `len(tool_definitions())==14`) 확인했습니다.
- **현재 리스크**: 스키마 11 마이그레이션이 기존 DB의 라우팅 테이블 3개를 DROP하므로 그 안의 과거 점수·샘플은 되돌릴 수 없습니다. `agent_hub_policy`의 `target="routing_prior"`와 응답의 `routing_decision`·`routing_prior` 필드가 사라졌으니 그걸 읽던 호출자는 깨집니다. 배포된 2.4.1 릴리스에는 1·2·3주차 변경이 모두 미반영입니다. `tests/agent_hub/test_connect_service.py::test_manager_close_clears_only_pending_login_it_started`가 CI에서 간헐적으로 실패합니다 — `ConnectionManager.close()`가 워커 스레드를 join하지 않아 생긴 기존 결함이며 1·2·3주차와 무관하고 별도 과제로 분리해 두었습니다.
- **Do-Not-Repeat**: `select_provider()`에 store나 과거 통계를 다시 넣지 마세요. 같은 입력이 같은 답을 준다는 성질이 이 교체의 유일한 이익입니다. 시스템이 자기 출력을 채점해 그 점수로 다음 행동을 정하는 루프를 다시 만들지 마세요. 이미지를 `inline_input`으로 나르지 마세요. 프롬프트 토큰 창에 계산돼 사진 한 장도 못 보냅니다. worker에 파일 경로를 넘기지 마세요. 샌드박스가 $HOME 아래 읽기를 막아 열 수 없습니다. 도구 스키마에 `{"type": "object"}`로 인자를 다시 숨기지 마세요. MCP 호출자에겐 그게 유일한 문서입니다.
- **다음 한 걸음**: `git push -u origin week3/routing-surgery`를 실행해 3주차 브랜치를 원격에 올리세요.
<!-- agent-hub:handoff:v1:end -->
