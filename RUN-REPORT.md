# Agent Hub 검증 기록

기준일: 2026-07-17

## 1.3.1 문서 문체 규칙 강제

다른 작업에서 예전 `Chanwoo-act/agent-hub` 체크아웃의 README를 다시 작성하면서 최신 문체 규칙이 적용되지
않는 문제를 재현했다. 최신 MCP는 `Meapri/agent-hub`의 `agent-hub-mono`에서 실행됐지만, 작성 대상 저장소의
규칙은 예전 `AGENTS.md`에서 읽었다. 여기에 직접 쓰기 도구가 자연스러운 한국어 검사기를 호출하지 않고,
workflow 검증도 `korean_style` 경고를 자동 재작성 조건으로 보지 않는 문제가 겹쳤다.

예전 체크아웃은 로컬 커밋 3개를 잃지 않도록 `/Users/naen/Git/agent-hub-legacy-chanwoo-20260717`과
`/Users/naen/Git/agent-hub-legacy-chanwoo-20260717.bundle`에 보존했다. 기존 `/Users/naen/Git/agent-hub`
경로는 `agent-hub-mono`를 가리키므로, 예전 경로로 새 작업을 열어도 Meapri 정본을 사용한다.

다음 경로를 함께 고쳤다.

- `agent_hub_write`가 모든 결과를 로컬 품질 검사에 통과시킨 뒤 반환
- 실패한 문서는 기본 1회, 최대 2회까지 전체를 다시 작성하고 남은 문제가 있으면 실패로 반환
- `quality_gate`에 통과 여부, 검사기 버전, 재작성 횟수, 정책 파일 기록
- `agent_hub_verify`의 내부 `ok=false`를 MCP 실패 신호로 전달
- workflow가 `korean_style`과 잘림 경고를 재작성 사유로 사용하고, 재작성 뒤에도 실패하면 run 중단
- README용 자연스러운 존댓말 프로필과 Codex·Claude Code 공용 `document-write` 스킬 추가
- 공용 스킬 동기화 스크립트를 특정 스킬 하나가 아니라 전체 스킬에 적용

예전 README는 새 검사에서 설명 없는 `콕핏`, `substrate`, `conductor`, `provider leaf`, `실행 패킷`을 찾아
실패했다. 현재 Meapri README는 같은 검사를 통과했다. 자동 검사는 Ruff, pytest
`275 passed, 11 skipped`, Ruler sync, Hub plugin sync, Phase 1 fixture, README 문체 검사와
`git diff --check`까지 통과했다. 버전은 `1.3.1`이다.

## 1.3 코드 조사·능동 추론·문서 품질 통합

### 재시작 뒤 심층 검증과 조사 방식 보강

Codex를 다시 시작한 뒤 Agent Hub MCP를 실제 호출했다. 첫 live status에서는 Claude와 Grok만 준비됐고
Gemini 액세스 토큰이 만료돼 `2/3 ready`였다. 수동 refresh 뒤에는 `3/3 ready`가 됐지만, 한 번의
`probe=true` 호출 안에서 provider probe가 토큰을 갱신하고도 갱신 전 로그인 상태를 반환하는 순서 문제가
확인됐다. status가 provider probe 뒤 로그인 파일을 다시 읽도록 바꾸고, Antigravity live status도
`agy_auth.status(probe=true)`로 갱신을 허용했다.

재시작한 MCP에서 Opus 4.8 planner가 만든 검증 plan은 아래 5단계였다.

1. Claude가 `deep`·`high`로 코드 조사
2. Claude와 Gemini가 조사 결과를 같은 wave에서 병렬 평가
3. Claude·Grok·Gemini가 `partially_supported` 여부를 병렬 판정
4. Claude가 모든 결과를 하나의 보고서로 정리

plan hash는 `42751ccc7b6c0def6358bb4c7b7e03d48e8bc2c53b67e9fc721912141d7834a0`, policy hash는
`ecf4db8227350488e85d01e4d1a3674325cad9741ea3c99ca15a6d21bb752d7a`였다. 실행은 4 wave, adaptive
step 5개로 끝났고 세 provider의 Consistency Gate는 `partially_supported`에 3/3 합의했다. 합의율과 응답
충족률은 모두 1.0이었으며 사람 확인으로 넘긴 항목은 없었다.

이 실행에서 기존 `deep` 조사의 한계도 드러났다. 전체 문맥은 18만 자였지만 파일마다 앞부분 5천 자만
보내서, 14KB인 `gather.py` 뒤쪽의 실제 수집 함수가 조사 모델에게 보이지 않았다. 그래서 수집기를 다음처럼
바꿨다.

- 1단계에서 entrypoint, 설정, 테스트, 스크립트, 구현 파일을 종류별로 섞어 넓게 확인
- 2단계에서 조사 요청의 파일명·함수명·기능을 기준으로 핵심 파일을 다시 선택
- 작은 핵심 파일은 전문을 전달하고, 큰 파일은 관련 함수 주변을 여러 구간으로 전달
- 모든 코드 조각에 원본 줄 번호와 `complete`·`partial` 상태 기록
- 결과 메타데이터에 전문 파일, 부분 파일, 실제 줄 범위 저장

같은 저장소와 조사 초점을 새 수집기에 넣은 로컬 재현에서는 후보 250개 중 57개 파일을 읽었다.
`src/orchestrate_codex/gather.py`, `document_quality.py`, `verify.py`, `catalog.py`는 전문이 들어갔고,
813줄인 `runner.py`는 관련 구간 4곳이 줄 번호와 함께 들어갔다. 이전 실행에서 보지 못한
`gather_code_context` 본문도 확인됐다.

보강 뒤 자동 검사는 Ruff, pytest `270 passed, 11 skipped`, Ruler sync, Hub plugin sync, Phase 1 fixture,
한국어 문서 검사까지 통과했다. 수정된 MCP 코드는 다음 앱 재시작 뒤 live workflow로 한 번 더 확인해야 한다.

README처럼 저장소 전체를 설명하는 글은 문장만 잘 다듬어서는 부족했다. 기존 adaptive planner가 로컬 코드
조사에 `search`를 고른 실험에서는 provider 웹 검색 경로가 실행돼, 조사 모델이 실제 심볼을 확인하지 못하고
`<경로 미확인>`을 남겼다. 추론 강도만 높이면 근거 없는 판단이 더 길어질 수 있으므로 로컬 코드 근거를
먼저 모으는 `inspect_codebase` capability를 추가했다.

planner가 새 plan에서 단계마다 아래 두 값을 고른다.

- `investigation_depth`: `shallow`, `standard`, `deep`
- `reasoning_effort`: `low`, `medium`, `high`

`deep` 조사는 entrypoint, 공개 도구와 스키마, 설정, 테스트, 생성 문서 동기화, Git 상태를 우선순위에 따라
수집한다. 조사 결과에는 실제로 읽은 파일과 전체 후보 수가 남는다. 모델별 추론 설정은 공식 API 계약에
맞춰 Claude `output_config.effort`, Grok Responses `reasoning.effort`, Gemini `thinking_level`로 전달한다.
지원하지 않는 모델은 값을 버리지 않고 실행을 거부한다. 구현 기준은
[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort),
[xAI reasoning](https://docs.x.ai/developers/model-capabilities/text/reasoning),
[Gemini thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking)에서 확인했다.

한국어 문서 규칙은 `instructions/.ruler/30-documents.md`에 넣어 Codex와 Claude Code에 함께 배포한다.
`orchestrate_codex.document_quality`는 이번에 반복된 번역투와 작업 중계 문장을 결정적으로 검사한다.
README 전용 테스트도 같은 공용 검사기를 사용하므로 금칙어 목록이 두 군데로 갈라지지 않는다.

### 실제 adaptive 실행

현재 작업 트리의 Agent Hub를 직접 실행했다. Claude Opus 4.8 planner가 아래 DAG를 한 번에 만들었다.

1. `inspect_orchestration`: Claude Opus 4.8, `reasoning_effort=high`, `investigation_depth=deep`
2. `write_doc`: Claude Opus 4.8, `reasoning_effort=high`, 첫 조사 결과에 의존

plan hash는 `a5c7b3ef41cc665ad281c9bbd20d91bd95e3882dab51befa77c61b5d2ca6c862`다. 두 단계는 의존 관계에
따라 두 wave로 실행됐고, leaf call 2회로 끝났다. 깊은 조사는 코드 후보 250개 가운데 30개를 읽었다.
`operations.py`, `orchestrator.py`, 세 provider MCP adapter, Ruler 정본, 대표 adaptive 테스트가 포함됐고
최종 문서에는 확인한 파일 경로가 근거로 남았다.

provider별 짧은 실호출도 확인했다.

| provider/model | 요청 | 실제 경로 | 결과 |
|---|---|---|---|
| Claude Opus 4.8 | `high` | adaptive inspection·write | 성공 |
| Grok 4.5 | `high` | `xai-responses-oauth`, 진단값 `reasoning_effort=high` | `EFFORT_OK` |
| Gemini 3.1 Pro High | `high` | `agy-oauth-code-assist`, capacity fallback 없음 | `EFFORT_OK` |

### 자동 검사

- Ruff: 통과
- Pytest: `270 passed, 11 skipped`
- Ruler sync: 통과
- Hub plugin 공통 스킬 sync: 통과
- README 문체 검사: 통과
- `git diff --check`: 통과

버전은 `1.3.0`으로 올렸다. 공개 MCP 도구 수는 26개, workflow 수는 5개로 그대로다. 새 capability는
adaptive plan 내부에서만 사용하므로 공개 도구를 더 늘리지 않았다. 새 코드를 적재한 앱 재시작 뒤 live
workflow 재검증이 남아 있다.

---

## 목표

provider 기능 확장 위에 LLM 판단형 orchestration과 결과 일관성 검증을 추가합니다. provider 순서를 코드에
고정하지 않고 planner가 실제 의존 관계를 만들며, 로컬 코드가 그 계획의 안전성과 실행 예산을 강제합니다.
Codex와 Claude Code 플러그인은 같은 Agent Hub MCP를 조작하는 얇은 진입점으로 유지합니다.

## 1.2 구현 결과

- `agent_hub_plan_workflow(workflow_id="adaptive")`: planner LLM이 provider와 dependency DAG를 제안합니다.
- 로컬 plan validator: 허용 목록, cycle, orphan, 단일 final sink, step/call budget을 검사합니다.
- dependency scheduler: 실행 가능한 frontier만 병렬로 처리하고, plan에 적힌 fallback을 적용합니다.
- canonical policy injection: provider마다 같은 프로젝트 규칙을 주입하고 정책·요청 hash를 기록합니다.
- Consistency Gate: 닫힌 decision label만 엄격한 JSON으로 비교하며 불일치와 부분 실패를 사람 검토로 돌립니다.
- 앱 플러그인: Codex·Claude Code 모두 같은 MCP와 공유 스킬을 사용하며 Claude Code 명령 2개를 제공합니다.
- diff review: untracked 파일은 기본 제외하며 adaptive review에서만 파일 수·크기·바이너리 제한과 함께 포함합니다.

## 실제 adaptive 실행 확인

Gemini 3.5 Flash High planner가 현재 변경 검토를 위해 아래 DAG를 만들었습니다.

1. 서로 의존하지 않는 Claude correctness review와 Grok regression review를 같은 실행 묶음에 배치했습니다.
2. 두 리뷰가 동시에 실행됐습니다.
3. Claude가 완료 표식을 내지 않아 해당 결과를 실패로 처리하고 Grok fallback으로 전환했습니다.
4. 두 완료 결과가 모인 다음 Gemini final synthesis가 실행됐습니다.

실행 결과는 `status=completed`, wave 2개, 실제 leaf call 4개였습니다. 이 결과는 “모델 응답이 무조건
정확하다”는 증거가 아니라, LLM이 고른 DAG·병렬 frontier·완료 계약·fallback이 실제 호출에서 연결됐다는
증거입니다. 리뷰 내용은 코드와 테스트로 다시 대조했으며, 확인된 untracked 위험만 반영했습니다.

Consistency Gate도 Claude, Grok, Gemini를 동시에 호출해 확인했습니다. 동일한 코드 근거와
`VERIFIED|NOT_VERIFIED` 선택지를 전달한 결과 3/3이 strict `decision_v1` 계약으로 `VERIFIED`를 반환했습니다.
합의율과 coverage는 모두 `1.0`, `provenance_consistent=true`였고 세 호출의 정책 hash와 요청 hash가 각각 하나로
일치했습니다. 이 검증은 닫힌 결정의 계약과 provenance가 작동한다는 뜻이며, 모든 자유 형식 답변이 항상
정답이라는 뜻은 아닙니다.

## 1.1 provider 확장 기준선

Gemini adapter에 몰려 있던 직접 작업을 Claude와 Grok으로 확장하면서도 공개 MCP 도구는 기존 26개
`agent_hub_*`로 유지합니다. 기능 이름은 provider와 분리하고, 실제 지원 범위는 schema와 capability 정보에서
확인할 수 있게 만드는 것이 목표입니다.

## 구현 결과

| 작업 | Claude | Grok | Gemini | Hub·로컬 |
|---|:---:|:---:|:---:|:---:|
| 대화·vision | ✓ | ✓ | ✓ | 이미지 입력 정규화 |
| 근거 검색 | ✓ | ✓ | ✓ | citation 형식 통합 |
| 글쓰기 | ✓ | ✓ | ✓ | 공통 prompt·품질 경고 |
| 이미지 생성 |  | ✓ | ✓ | 결과 로컬 캐시 |
| 모델 비교 | ✓ | ✓ | ✓ | 다중 provider 실행 |
| Git diff 검토 | ✓ | ✓ | ✓ | diff 수집 |
| 릴리스 스냅샷 |  |  |  | ✓ |
| 릴리스 문서 | ✓ | ✓ | ✓ | Git 사실 수집·선택적 윤문 |

주요 구조 변경:

- `src/agent_hub/capabilities.py`: adapter가 실제 구현한 기능과 제한을 한곳에서 관리합니다.
- `src/agent_hub/core/media.py`: 로컬 경로, data URL, 공개 HTTPS 이미지 입력을 안전하게 정규화합니다.
- `src/agent_hub/provider_settings.py`: Claude·Grok 기본 모델과 호출 옵션을 저장합니다.
- `src/claude_codex/search.py`: Anthropic native web search와 citation을 처리합니다.
- `src/grok_codex/search.py`: xAI web search·X search와 citation을 처리합니다.
- `src/grok_codex/image.py`: Grok Imagine 결과를 검증하고 로컬에 저장합니다.

## 실제 호출 확인

현재 로컬 subscription OAuth를 사용해 다음 경로를 짧게 확인했습니다.

| 확인 항목 | 결과 |
|---|---|
| Claude Sonnet 5 이미지 이해 | 첨부 이미지 제목 `지원 범위` 반환 |
| Grok 4.5 이미지 이해 | 첨부 이미지 제목 `지원 범위` 반환 |
| Claude native web search | 공식 URL과 citation 반환 |
| Grok native web search | 공식 URL과 citation 반환 |
| Claude·Grok 모델 비교 | 두 provider 모두 `1+1 → 2` 응답 |
| 로컬 릴리스 스냅샷 | `provider=local`, branch `main` 반환 |

첫 비교 호출에서는 Claude Sonnet 5가 deprecated `temperature` 옵션을 거부했습니다. adapter가 Claude 5 계열에서
이 옵션을 제거하도록 수정한 뒤 같은 호출을 다시 실행해 성공을 확인했습니다.

## 자동 검증

- Ruff: 통과
- Pytest: `246 passed, 11 skipped`
- Ruler sync: 통과
- Phase 1 disposable fixture: 통과
- `doctor.sh`: 5/5 통과
- Hub plugin 공통 스킬 sync 검사: 통과
- Claude Code plugin manifest: `claude plugin validate` 통과
- Codex app plugin: `agent-hub@agent-hub 1.2.0`, `installed, enabled` 확인
- Claude Code app plugin: 사용자 범위 `agent-hub@agent-hub 1.2.0`, `enabled` 확인
- sdist·wheel: `agent_hub-1.2.0` 빌드 성공
- 새 Python 3.14 가상환경에 wheel 단독 설치: 버전 `1.2.0`, 공개 도구 26개, workflow 5개,
  `adaptive=True` 확인
- 시스템 Python 3.9 설치 시도: `Requires-Python >=3.10` 조건으로 의도대로 거부
- 공개 도구: 26개, 중복 없음, 전부 `agent_hub_*`
- provider schema: 검색·글쓰기·diff·릴리스 문서는 Claude/Grok/Gemini, 이미지 생성은 Grok/Gemini
- 릴리스 스냅샷 schema: provider 필드 없음

## 의도적으로 남긴 경계

- Grok 실제 이미지 생성은 호출당 비용이 발생할 수 있어 자동 live smoke에서 제외했습니다. HTTP 계약,
  base64 응답 처리, 파일 저장은 mock 테스트로 확인했습니다.
- provider의 기능 구현과 계정별 API entitlement는 다릅니다. `agent_hub_status`의 readiness와 실제 호출 결과를
  함께 봐야 합니다.
- 자동 비교는 일부 provider가 실패해도 성공한 결과를 보존하고 `partial_compare_failures` warning을 남깁니다.
