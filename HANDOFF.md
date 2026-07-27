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
- **원래 목표**: 공개 진입점에서 도달할 수 없는 코드를 지우고, 생성된 이미지를 실제 바이트로 저장하며, claude 인증 lane의 조용한 교체를 드러냅니다. 4주 계획의 4주차입니다.
- **현재 단계**: 4주차 네 항목 중 세 개를 끝냈고, provider MCP 중복층은 측정만 하고 별도 과제로 분리했습니다. `week4/delete-duplicates` 브랜치에 커밋 3건이 있고 push 전입니다.
- **완료**:
  - `src/agent_hub/v2/sdk.py` 345줄과 workflow JSON 5개 116줄을 지웠습니다. console_scripts 전 진입점에서 시작한 import 그래프 탐색이 111개 모듈에 닿는데 둘 다 그 안에 없었고, CLI 하위명령도 14개 MCP 도구도 건드리지 않았으며, service.py와 store.py에 "workflow" 문자열이 0회 나옵니다. 그런데도 JSON은 wheel에 포함돼 배포되고 있었습니다.
  - 에러 코드 4개(unsupported_workflow_schema, invalid_workflow, mock_method_unavailable, invalid_package_digest)가 함께 사라졌습니다. sdk.py 안에서만 raise됐고 자기 테스트에서도 미커버였습니다.
  - `test_replan_preserves_completed_steps_and_replaces_only_pending`은 삭제하지 않고 store 테스트로 옮겼습니다. `store.replace_pending_plan`을 검증하는 살아 있는 테스트인데 sdk 헬퍼 옆에 앉아 있었을 뿐입니다.
  - 생성된 이미지를 실제 바이트로 저장합니다. 이전에는 artifact에 "Generated image: /Users/you/.cache/grok-codex/images/grok_abc.png" 문장이 text/plain으로 들어갔습니다. 파일에 대한 메모이지 파일이 아니며, content_sha256은 경로 문자열의 해시라 무결성 검증이 그림에 대해 아무것도 말해주지 못했습니다.
  - 그 캐시 디렉터리를 정리하는 코드가 저장소에 하나도 없어 이미지가 무한히 쌓이고 있었습니다. 이제 worker가 자기가 쓴 파일을 읽어 base64로 실어 보내고 즉시 지웁니다.
  - 읽기를 daemon이 아니라 worker에서 하는 이유는 샌드박스를 가드로 유지하기 위해서입니다. 그리고 보고된 경로를 provider 자기 이미지 디렉터리와 대조한 뒤에 읽으므로, provider 응답이 임의 파일 읽기로 바뀌지 않습니다.
  - `agent_hub_execute`가 provider envelope를 그대로 반환하므로 호출자에게 사용자 홈의 절대 경로가 넘어가고 있었습니다. 이제 경로가 응답에 남지 않습니다.
  - artifacts 컬럼은 원래 BLOB이고 cipher도 바이트 지향이었습니다. 막고 있던 것은 모든 읽기가 UTF-8 디코딩 하나를 거친다는 점뿐이었습니다. `_artifact_bytes`를 `_artifact_text` 아래에 두고, export는 저장된 바이트를 그대로 쓰며(디코딩 후 재인코딩은 어떤 이미지도 살아남을 수 없는 왕복입니다), 검증은 인코딩이 아니라 다이제스트를 봅니다.
  - `agent_hub_artifact`의 기존 get 액션에 `include_base64` 플래그를 더했습니다. 새 도구 없이 14개 그대로이며, 4MB에서 끊어 전송 계층의 daemon_response_too_large 대신 prepare_export를 가리키는 artifact_too_large가 나오게 했습니다.
  - media type을 어댑터의 추측이 아니라 파일 확장자에서 가져옵니다. grok 어댑터는 .png가 아니면 무조건 image/jpeg로 보고해서 저장된 .webp가 JPEG로 광고되고 있었습니다.
  - claude 인증 lane 교체를 드러냅니다. `CLAUDE_CODEX_AUTH_MODE=api_key`인데 키가 없으면 구독으로 되돌아가는데, 반환값이 선택된 구독과 구분되지 않았고 status()도 교체된 lane을 선호처럼 보고했습니다. 두 lane은 과금 대상과 헤더가 다르므로 사용자가 알아채는 첫 지점이 청구서였습니다. 이제 `requested_mode`/`lane_substituted`를 싣고 provider status가 `auth_lane_substituted` 경고를 냅니다.
  - lane 테스트가 저장소 전체에 1건(구독 우선 happy path)뿐이었습니다. 재폴백, api_key 모드, OAuth 모양 키 분기, config 파일 키를 덮는 테스트 9건을 추가했습니다. 파일 lane이 실무상 가장 중요합니다 — 샌드박스 worker의 환경변수 allowlist가 ANTHROPIC_API_KEY를 통과시키지 않아 durable run 안에서는 파일이 유일하게 동작하는 API key 경로입니다.
- **미완**: provider MCP 중복층 정리가 남았습니다. 측정 결과 파일 4개 2,228줄 중 약 1,573줄이 아무도 실행하지 않는 프로토콜 껍데기이고, provider_runtime은 MCP 프로토콜을 전혀 타지 않고 같은 프로세스의 `dispatch_tool()` 함수를 부를 뿐입니다. 그러나 `dispatch_tool`이 진입점이면서 예외를 `success:False` payload로 바꾸는 변환기를 겸하고 있고 `_raise_failed_payload`가 정확히 그 모양에 의존하므로, 삭제가 아니라 추출 작업입니다. 별도 과제로 분리했습니다. 브랜치를 아직 push하지 않았습니다.
- **변경 파일**: 신규 `tests/agent_hub/test_v2_image_output.py`, `tests/claude_codex/test_auth_lanes.py`. 삭제 `src/agent_hub/v2/sdk.py`, `src/agent_hub/v2/workflows/`, `tests/agent_hub/test_v2_sdk_replan.py`. 수정 `src/agent_hub/v2/provider_worker.py`, `provider_runtime.py`, `service.py`, `tools.py`, `src/claude_codex/auth.py`, `tests/agent_hub/test_v2_store.py`, `pyproject.toml`.
- **검증 실행 결과**: 전체 pytest `752 passed, 2 skipped`; `ruff check` 통과; `ruff format` 적용; `./scripts/check-sync.sh` 통과; 공개 도구 14개 불변식 유지(`len(TOOL_NAMES)==14`, `len(tool_definitions())==14`) 확인했습니다.
- **현재 리스크**: 이미지 artifact의 media_type이 이제 text/plain이 아니므로 artifact를 무조건 텍스트로 읽던 호출자는 `artifact_not_text`를 받습니다. `agent_hub_execute`의 image 응답에서 `data.path`와 `data.image`가 사라졌으니 그 경로로 파일을 열던 호출자는 깨집니다. 기존에 쌓인 provider 캐시 이미지는 이번 변경이 지우지 않습니다 — 신규 생성분만 정리됩니다. 배포된 2.4.1 릴리스에는 1·2·3·4주차 변경이 모두 미반영입니다. `tests/agent_hub/test_connect_service.py::test_manager_close_clears_only_pending_login_it_started`의 CI 간헐 실패는 여전히 남아 있고 별도 과제입니다.
- **Do-Not-Repeat**: 이미지 경로를 daemon에서 읽지 마세요. 경로는 provider 응답에 실려 오는 값이라, 검증 없이 읽으면 provider 응답이 임의 파일 읽기가 됩니다. artifact 읽기를 다시 UTF-8 디코딩 하나로 합치지 마세요. 바이너리 artifact가 모든 경로에서 다시 읽을 수 없게 됩니다. export에서 텍스트 왕복을 되살리지 마세요. 이미지가 깨집니다. claude 인증 재폴백을 조용하게 되돌리지 마세요. 사용자가 lane 교체를 청구서로 알게 됩니다. provider MCP 계층을 `dispatch_tool`의 예외 변환을 옮기기 전에 지우지 마세요. 실패 분류가 그 payload 모양에 의존합니다.
- **다음 한 걸음**: `git push -u origin week4/delete-duplicates`를 실행해 4주차 브랜치를 원격에 올리세요.
<!-- agent-hub:handoff:v1:end -->
