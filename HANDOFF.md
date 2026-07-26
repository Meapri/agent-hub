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
- **원래 목표**: Agent Hub 자체의 코드와 공개 계약을 근거로, 특징·강점·구조를 쉽게 설명하는 자연스러운 한국어 README를 처음부터 다시 작성하고 검증된 결과를 GitHub에 반영합니다.
- **현재 단계**: Agent Hub document-write run과 로컬 검증을 마쳤으며 `README.md`와 이 인계 기록을 같은 커밋으로 `main`에 반영하는 단계입니다.
- **완료**:
  - `agent_hub_plan`의 승인된 fact pack으로 저장소 구조와 공개 계약을 조사하고 durable run `adb57d0b406f8ff6` revision 16을 완료했습니다.
  - Claude `claude-opus-5`가 초안과 최종 문서를 작성하고 Gemini `gemini-3.6-flash-high`가 사실성·구성·문체를 검토했습니다.
  - 최종 Agent Hub artifact `art_21384b76dc0ce3ca07a901f3`의 content digest `5666a23e792e8cdeca53f38cd2274beae7915cd29ba72c2f0cfed761b8811e16`과 암호화 content 인증을 확인했습니다.
  - 기존 475줄 README를 340줄로 재구성해 주요 특징, 빠른 시작, 구조, provider 상태, 실행 흐름, 14개 MCP 도구, 라우팅·정책, 보안, artifact·HANDOFF, 복구, 지원 범위를 쉬운 문장으로 정리했습니다.
  - 검증 완료 후 `final_readme` step에 `verified`, rating 5 feedback을 기록했습니다.
- **미완**: 저장소 전체 Ruff format 기준선에는 기존 Python 파일 79개의 포맷 차이가 남아 있습니다. README 작업 중 발견한 대형 dependency artifact 중복 전달 문제도 후속 수정이 필요합니다.
- **변경 파일**: `README.md`, `HANDOFF.md`를 변경합니다.
- **검증 실행 결과**: README user-facing verify와 document quality를 통과했습니다. `tests/agent_hub/test_readme_copy.py`와 `tests/agent_hub/test_hub_plugins.py`는 12 passed, Ruler sync·Hub plugin sync·release version 2.1.4·`git diff --check`를 통과했습니다. 전체 pytest는 README 재적용 전 실행에서 558 passed, 2 skipped였고 package build도 성공했습니다. 전체 Ruff check는 통과했지만 `ruff format --check src tests`는 기존 79개 Python 파일 때문에 실패했습니다.
- **현재 리스크**: 첫 광범위 plan은 중복된 대형 inspect artifact를 downstream provider에 전달해 실패했습니다. 축약 plan은 성공했지만 `src/agent_hub/v2/service.py`에 dependency artifact 중복 제거와 provider context 크기 제한이 없어 같은 문제가 다시 생길 수 있습니다. 또한 전체 pytest와 build를 병렬 실행한 뒤 미커밋 README가 HEAD 내용으로 돌아온 현상을 관측했으나 원인 command는 아직 특정하지 못했습니다.
- **Do-Not-Repeat**: active external step에 중복 continue를 보내지 마세요. 대형 동일 source scope의 inspect step을 여러 개 만들지 마세요. 원인을 격리하기 전에는 미커밋 작업이 있는 현재 checkout에서 전체 pytest와 package build를 병렬 실행하지 마세요. 기존 79개 파일을 README 작업에 섞어 자동 포맷하지 마세요.
- **다음 한 걸음**: `src/agent_hub/v2/service.py`의 downstream artifact 조립 경로에 동일 fact pack digest 중복 제거와 provider context 크기 검사를 추가하고 `tests/agent_hub/test_v2_service.py`에 대형 중복 inspect artifact 회귀 fixture를 작성하세요.
<!-- agent-hub:handoff:v1:end -->
