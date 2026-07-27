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
- **원래 목표**: 4주 계획 이후 남은 세 가지를 끝냅니다. CI를 깨던 스레드 누수 수정, provider MCP 중복층 정리, 버전 올리기입니다.
- **현재 단계**: 세 가지를 모두 처리하고 3.0.0으로 올렸습니다. `week5/finish-the-plan` 브랜치에 커밋 4건(`f9e7898`, `f958a5f`, `f2c6504`, `51099b9`)이 있고 push 전입니다.
- **완료**:
  - `ConnectionManager.close()`가 워커 스레드를 실제로 기다립니다. 이전에는 "stop local helpers"라고 하면서 cancel event만 set하고 반환했고, 시작한 스레드 6개를 추적하지도 join하지도 않았습니다. 그 스레드들은 pending OAuth flow를 지우고 토큰을 쓰는 프로세스 전역 상태를 건드립니다.
  - 그게 CI 간헐 실패의 원인이었습니다. 앞 테스트가 남긴 워커가 뒤 테스트가 monkeypatch한 `grok_oauth.clear_pending_login`을 불러, 앞 테스트의 flow id가 뒤 테스트 assertion에 들어갔습니다. 테스트는 증상이고 계약이 결함이었습니다.
  - 스레드를 시작할 때 등록하고, close()가 공유 데드라인 안에서 join합니다. 데드라인을 넘긴 워커는 무한히 기다리지 않고 로그로 남깁니다. daemon 스레드라 프로세스를 붙잡지는 않지만, close()가 끝냈다고 믿는 호출자에겐 알려야 합니다.
  - 테스트 9건이 close()를 워커가 기다리는 이벤트보다 먼저 불렀고, 그건 close()가 안 기다렸기 때문에만 성립했습니다. 타이머로 release하도록 바꿔 검증 대상(워커가 진행 중일 때 닫기)을 유지했습니다. connect 스위트가 19.4초에서 0.9초가 됐습니다.
  - provider 실패 payload에서 예외 문자열을 끊었습니다. openai_codex만 코드를 고정 문장으로 매핑했고 claude_codex와 grok_codex는 `str(exc)`를 `text`와 `error` 양쪽에 넣고 있었습니다. HTTP 클라이언트의 예외 문자열은 요청 URL·헤더 조각·파일 경로를 담을 수 있고, 이 payload는 호출자에게 돌아갑니다.
  - 변환기를 `agent_hub.core.response.failure_payload` 하나로 모았습니다. `error_type`은 그대로 예외에서 오며(v2 실패 분류가 그걸 읽습니다), 사람이 읽는 쪽만 예외에서 떼어냈습니다.
  - v2가 provider mcp_server를 전혀 import하지 않습니다. `dispatch_tool` 호출 7곳을 leaf 직접 호출로 바꿨습니다. v2는 원래 MCP를 말한 적이 없었고, 같은 프로세스에서 dict 조회 후 어댑터를 부르는 함수를 호출했을 뿐입니다. gemini 경로의 MCP 포장→해체 왕복도 사라졌습니다.
  - 3.0.0으로 올렸습니다. minor가 아닌 이유는 2.4.1 기준으로 작성된 호출자가 깨질 수 있기 때문입니다. 버전 선언 6곳을 모두 갱신했습니다.
  - README가 삭제된 라우팅 계층을 상세히 설명하고 있어(프로필 가중치 표, prior 파일, evidence kind) 실제 선택 동작으로 교체하고, 문서가 아예 없던 이미지 처리를 새로 썼습니다.
- **미완**: provider MCP 프로토콜 계층 2,228줄은 삭제하지 않았습니다. 앞서 "아무도 실행하지 않는다"고 기록했는데 틀렸습니다. `scripts/claude_codex_mcp.py`에 initialize를 보내 유효한 응답을 받아 launcher가 실제로 동작함을 확인했습니다. plugin README가 보관용 snapshot이라고 표시할 뿐 고장난 것은 아니므로, 지우는 것은 정리가 아니라 제품 결정이며 사용자 판단이 필요합니다. 브랜치를 아직 push하지 않았습니다.
- **변경 파일**: 신규 `tests/agent_hub/test_provider_failure_payload.py`. 수정 `src/agent_hub/connect_service.py`, `src/agent_hub/core/response.py`, `src/agent_hub/v2/provider_runtime.py`, `src/claude_codex/mcp_server.py`, `src/grok_codex/mcp_server.py`, `src/openai_codex/mcp_server.py`, `tests/agent_hub/test_connect_service.py`, `tests/agent_hub/test_v2_provider_runtime.py`, `tests/openai_codex/test_core.py`, `README.md`, `pyproject.toml`, `src/agent_hub/__init__.py`, `hubs/*/plugin.json`, `.claude-plugin/marketplace.json`.
- **검증 실행 결과**: 전체 pytest `761 passed, 2 skipped`; `ruff check` 통과; `ruff format` 적용; `./scripts/check-sync.sh` 통과; `orchestrate_codex.verify --user-facing README.md` 통과; `orchestrate_codex.document_quality README.md` 통과; 버전 선언 3곳이 3.0.0으로 일치함을 확인했습니다. 이전에 간헐 실패하던 테스트 쌍을 15회 반복해 전부 통과했습니다.
- **현재 리스크**: 3.0.0은 2.4.1 호출자를 깨뜨립니다. `agent_hub_policy`의 `target="routing_prior"`, 응답의 `routing_decision`·`routing_prior`, 정책의 `routing_profile`이 없어졌고, 스키마 11 마이그레이션은 단방향이며, 이미지 artifact가 바이너리라 무조건 텍스트로 읽던 호출자는 `artifact_not_text`를 받습니다. provider 실패 메시지 문구가 바뀌었으니 문구를 매칭하던 코드는 깨집니다. 아직 배포하지 않았으므로 설치된 런타임은 여전히 2.4.1입니다.
- **Do-Not-Repeat**: `close()`가 스레드를 기다리지 않도록 되돌리지 마세요. 남은 스레드가 전역 OAuth 상태를 건드려 테스트가 다시 플래키해집니다. 실패 payload에 `str(exc)`를 다시 넣지 마세요. URL과 경로가 호출자에게 나갑니다. `error_type`을 고정 문자열로 바꾸지 마세요. v2 실패 분류가 그 값을 읽습니다. provider MCP 계층을 "죽은 코드"라고 전제하고 지우지 마세요. launcher가 동작함을 확인했습니다.
- **다음 한 걸음**: `git push -u origin week5/finish-the-plan`을 실행해 3.0.0 브랜치를 원격에 올리세요.
<!-- agent-hub:handoff:v1:end -->
