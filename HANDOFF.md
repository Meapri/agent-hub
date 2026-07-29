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
- **원래 목표**: 이 저장소가 실제로 어떻게 깨져 왔는지에 근거해 Agent Hub를 개선합니다.
- **현재 단계**: 3.1.1을 병합(`c75c31f`)하고 설치했습니다. daemon과 bridge 모두 `3.1.1-9864146ea13d`이고 doctor 7/7 pass, store schema 11입니다. 진행 중인 작업은 없습니다.
- **완료**:
  - 3.1.0: 실제 provider를 부르는 canary 19건(`AGENT_HUB_LIVE=1 pytest -m live`), 응답의 의미를 `agent_hub.core.response.chat_outcome` 한 곳에서 정하기, `agent_hub_plan` 스키마 문서화와 강제 검증, 아무도 부르지 않는 정의 22개 399줄 삭제입니다.
  - canary를 쓰다가 둘을 잡았습니다. 하위 디렉터리 conftest의 `pytest_collection_modifyitems`는 모든 항목을 받아서 첫 버전이 스위트 전체를 건너뛰었고, claude는 확장 사고로 출력 예산을 전부 쓰고 텍스트 0자를 반환하는데 그게 평범한 성공으로 나가고 있었습니다.
  - 3.1.1: 이 기록을 갱신하려다 `apply_update`가 `internal_error`만 돌려주는 것을 겪었습니다. 원인은 `file`을 빼먹은 것인데, 인자 모양 오류가 평범한 `ValueError`라 런타임이 정체불명의 내부 실패로 감쌌습니다. 이 기계의 통계로도 handoff 호출 76건 중 12건이 실패했고 그중 6건이 `internal_error`였습니다.
  - `HandoffArgumentError(field, limit)`를 만들어 인자 모양 오류 9곳을 옮겼고, 런타임이 이를 `invalid_request`로 바꾸면서 고쳐야 할 필드 이름과 한계값을 함께 돌려줍니다. 메시지를 그대로 내보내도 되도록, 모든 메시지가 리터럴인지 AST로 검사합니다.
  - `agent_hub_handoff`의 `arguments`가 빈 object였습니다. 여섯 action이 읽는 16개 인자를 전부 문서화하고 `additionalProperties: false`로 닫았습니다. 런타임이 실제로 읽는 키 집합과 문서가 어긋나면 테스트가 깨집니다.
- **미완**: provider MCP 프로토콜 계층 2,228줄은 그대로입니다. v2는 이제 전혀 import하지 않지만 `scripts/*_mcp.py` launcher가 실제로 동작하므로 삭제는 사용자 판단이 필요합니다.
- **변경 파일**: 3.1.1에서 신규 `tests/agent_hub/test_handoff_discoverability.py`. 수정 `src/agent_hub/core/handoff.py`, `src/agent_hub/v2/service.py`, `src/agent_hub/v2/tools.py`, 그리고 버전 문자열 5곳(`pyproject.toml`, `src/agent_hub/__init__.py`, `README.md`, `.claude-plugin/marketplace.json`, 두 hub plugin.json).
- **검증 실행 결과**: 전체 pytest `853 passed, 2 skipped`; `ruff check` 통과; 작업한 4개 파일 format 통과; `./scripts/check-sync.sh` 통과; README verify·document_quality 통과; 공개 도구 14개 유지. 고침을 되돌려 새 테스트가 실패하는 것을 확인했습니다(`file` 인자 1건, 스키마 문서 4건). 설치된 3.1.1에 대해 `AGENT_HUB_LIVE=1 pytest -m live` 19 passed(96초), 설치 후 daemon에 `file` 없는 apply_update를 보내 `invalid_request`와 `safe_details.field=file`이 돌아오는 것을 확인했습니다.
- **현재 리스크**: `additionalProperties: false`는 문서에 없는 인자를 호출자 쪽에서 거절하게 만듭니다. 런타임이 읽는 키와 문서가 일치하는 것은 테스트로 막았지만, 새 인자를 추가하면서 문서를 빼먹으면 그 인자는 호출조차 되지 않습니다. live canary는 돈이 들고 자격증명이 필요해 CI에서 돌지 않으므로 사람이 때때로 돌려야 합니다. 로그인이 만료된 provider는 실패가 아니라 건너뛰므로 전부 건너뜀을 전부 통과로 오해하지 않아야 합니다. gh 활성 계정이 세션 중 두 번 `Chanwoo-act`로 바뀌어 push가 거절됐고 그때마다 `Meapri`로 되돌렸습니다.
- **Do-Not-Repeat**: 인자 모양 오류를 맨 `ValueError`로 던지지 마세요. 호출자에게 `internal_error`로 도착해 무엇을 고쳐야 하는지 알려주지 못합니다. 공개 도구의 중첩 인자를 빈 object로 두지 마세요. MCP 호출자는 스키마 말고는 볼 것이 없습니다. 응답의 의미(잘림·빈 답·성공 여부)를 어댑터에서 다시 정하지 마세요. 같은 버그가 셋에 생기고 하나만 고쳐도 테스트가 통과합니다. 스키마 설명을 강제 코드 검증 없이 추가하지 마세요. 하위 디렉터리 conftest의 `pytest_collection_modifyitems`에서 범위 검사를 빼지 마세요. 전체 스위트가 조용히 건너뛰어집니다. 죽은 코드를 AST만 보고 지우지 마세요. 이름으로 디스패치되는 것이 있습니다. 실험 삼아 되돌린 파일을 `git checkout`으로 복구하지 마세요. 같은 파일의 작업 중인 편집까지 사라집니다.
- **다음 한 걸음**: `AGENT_HUB_LIVE=1 ./.venv/bin/python -m pytest -m live -v`를 하루 뒤에 다시 실행해 gemini 토큰이 1시간 만료 뒤에도 자동 갱신되는지 확인하세요.
<!-- agent-hub:handoff:v1:end -->
