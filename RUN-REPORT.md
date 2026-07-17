# Agent Hub 1.1 provider capability 확장 결과

기준일: 2026-07-17

## 목표

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
- Pytest: `220 passed, 11 skipped`
- Ruler sync: 통과
- Phase 1 disposable fixture: 통과
- `doctor.sh`: 5/5 통과
- sdist·wheel: `agent_hub-1.1.0` 빌드 성공
- 새 Python 3.14 가상환경에 wheel 단독 설치: 버전 `1.1.0`, 공개 도구 26개, 신규 모듈 import 확인
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
