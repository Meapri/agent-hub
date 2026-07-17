# Agent Hub 1.2 adaptive orchestration·일관성 검증 결과

기준일: 2026-07-17

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

1. Claude correctness review와 Grok regression review를 같은 dependency frontier에 배치했습니다.
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
