# Agent Hub

Claude, Grok, Gemini, GPT를 Codex와 Claude Code에서 같은 방식으로 쓰기 위한 개인용 멀티 모델 MCP 서버입니다.

모델마다 도구 이름이 다르고, 로그인 절차가 다르고, 설정을 저장하는 위치까지 흩어져 있는 상황을 로컬 서버 하나로 정리했습니다. 짧은 질문은 그대로 던지고, 조사·작성·검토가 섞인 큰 작업은 여러 모델이 참여하는 workflow(작업 흐름)로 나눠 실행할 수 있습니다. 프로젝트 규칙과 인계 문서 `HANDOFF.md`를 함께 쓰기 때문에 Codex에서 시작한 작업을 Claude Code에서 이어받기도 수월합니다.

- 패키지 이름: `agent-hub`
- 버전: **1.4.3** (`pyproject.toml`, `src/agent_hub/__init__.py`)
- 필요 Python: **3.10 이상**
- 라이선스: MIT
- 런타임 의존성: **없음**

> Agent Hub는 Anthropic, xAI, Google, OpenAI의 공식 제품이 아닙니다. 모든 모델·provider 호출에는 사용자의 명시적 동의가 필요하고, 그 동의는 provider별 adapter(각 모델과 실제로 통신하는 연결 모듈)가 직접 강제합니다(`NOTICE.md`).

---

## 어떤 문제를 해결하나요

여러 AI 코딩 도구를 함께 쓰다 보면 다음 세 가지가 계속 발목을 잡습니다.

1. **인터페이스가 제각각입니다.** provider를 바꾸면 도구 이름, 입력 필드, 응답 형태가 전부 달라집니다.
2. **인증이 흩어집니다.** OAuth 로그인, 키체인 미러링, 구독 확인이 provider마다 다른 경로에 놓여 있습니다.
3. **긴 작업이 한 번의 호출로 끝나지 않습니다.** 조사 → 작성 → 검토를 한 프롬프트에 몰아넣으면 근거가 얕아지고, 중간에 끊기면 처음부터 다시 해야 합니다.

Agent Hub는 이 세 가지를 각각 **하나로 고정된 공개 도구 표면**, **로컬 GUI 연결 관리자**, **상태를 저장하는 workflow 실행기**로 나눠 다룹니다.

---

## 특징 요약

- 공개 인터페이스는 provider와 관계없이 `agent_hub_*` 37개로 고정됩니다. provider별 내부 도구(leaf, 각 모델 전용 하위 MCP 모듈)는 공개 표면에 나타나지 않습니다.
- Claude, Grok, Gemini, GPT 네 연결을 registry(provider 등록 목록) 하나에서 관리하고, 어떤 기능을 지원하는지 코드가 직접 선언합니다.
- 로그인·로그아웃 같은 인증 변경은 MCP 도구가 아니라 로컬 GUI(`agent-hub-connect`)에서만 일어납니다.
- 문서 작성 도구에는 실패 시 결과를 막아 세우는 품질 게이트가 있어, 통과하지 못한 초안을 성공으로 돌려주지 않습니다.
- 프로젝트 규칙(`AGENTS.md`, `CLAUDE.md`)은 `instructions/.ruler/` 정본 하나에서 만들어 두 도구에 배포합니다.
- 외부 런타임 의존성이 없어 표준 Python 가상환경만으로 설치가 끝납니다.

---

## 설치

저장소를 clone한 위치에서 가상환경을 만들고 편집 가능 모드로 설치해 주세요.

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
```

개발 도구가 필요 없다면 `./.venv/bin/pip install -e .`처럼 dev extra 없이 설치해도 됩니다. dev extra에는 `build>=1.2`, `pytest>=8`, `pytest-cov>=5`, `ruff>=0.12`가 들어 있습니다(`pyproject.toml`).

### 설치되는 실행 명령 8개

설치가 끝나면 가상환경의 `bin/` 아래에 다음 명령이 생깁니다(`pyproject.toml`).

| 명령 | 역할 |
| --- | --- |
| `agent-hub-mcp` | 통합 MCP 서버 실행 |
| `agent-hub-setup` | 로컬 MCP 설정 반영 |
| `agent-hub-doctor` | 로컬 상태 진단 |
| `agent-hub-connect` | 로컬 연결 관리 GUI |
| `claude-codex-consent` | Claude 동의 CLI |
| `grok-codex-consent` | Grok 동의 CLI |
| `google-antigravity-consent` | Gemini 동의 CLI |
| `openai-codex-consent` | GPT 동의 CLI |

`scripts/` 아래 파일은 실행 명령으로 등록되어 있지 않습니다. 예를 들어 Claude 로그인 보조 스크립트는 `./.venv/bin/python scripts/claude_codex_login.py` 형태로 직접 실행해 주세요.

### 로컬 MCP 경로 설정

MCP 설정 파일에는 clone 위치와 가상환경의 절대경로가 들어가야 하므로, 설치 후 `agent-hub-setup`으로 반영합니다. 지원 옵션과 미리보기·적용 구분은 실행 전에 도움말로 확인하시길 권합니다.

```bash
./.venv/bin/agent-hub-setup --help
./.venv/bin/agent-hub-setup          # 변경 계획만 확인
./.venv/bin/agent-hub-setup --apply  # 확인한 계획 적용
```

기본 실행은 read-only dry-run입니다. `--apply`를 줘야 로컬 MCP와 plugin 설정을 원자적으로
반영하며, provider 로그인·로그아웃이나 네트워크 호출은 대신 수행하지 않습니다.

프로젝트 규칙과 생성 문서를 맞추는 스크립트는 다음 두 개입니다.

```bash
./scripts/sync.sh        # instructions/.ruler 정본에서 생성물 배포
./scripts/check-sync.sh  # 생성물이 정본과 어긋났는지 검사
```

### Codex와 Claude Code에 plugin 등록

`/absolute/path/to/agent-hub`를 실제 clone 경로로 바꿔 실행해 주세요.

```bash
# Codex
codex plugin marketplace add /absolute/path/to/agent-hub
codex plugin add agent-hub@agent-hub
codex plugin list

# Claude Code
claude plugin marketplace add /absolute/path/to/agent-hub
claude plugin install agent-hub@agent-hub --scope user
claude plugin list
```

### 공유 메모리(선택)

`memory/`는 로컬 노트 저장소입니다. 실행 방식이 `uvx basic-memory mcp`로 문서화되어 있어 `uv`가 필요하고, 노트는 `memory/data/`에 저장되며 런타임 파일은 Git에서 제외됩니다. 네트워크 사용을 없애기 위해 `BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED=false`로 의미 검색을 끄도록 안내합니다(`memory/README.md`).

---

## 연결 관리: 로그인과 모델 선택

### MCP 밖에서 사용자가 직접 연결합니다

인증 상태를 **바꾸는** 일은 MCP 도구가 아니라 사용자가 로컬 GUI나 CLI에서 직접 실행합니다.
`agent_hub_auth_start`나 `agent_hub_auth_logout`을 호출하면 서버는 작업을 수행하지 않고
`success=false`와 `provider_gui_required` 오류, `next_action.type="local_gui"`, 그리고 실행해야 할
`agent-hub-connect`의 절대경로를 돌려줍니다. 이 응답에 인증 문자열이 섞여 나가지 않도록 회귀
테스트가 걸려 있습니다(`tests/agent_hub/test_provider_expansion.py`).

```bash
./.venv/bin/agent-hub-connect
```

GUI 서버는 다음과 같이 동작합니다.

- `127.0.0.1`에만 바인딩합니다(`src/agent_hub/connect_app.py`).
- 기동할 때 무작위 세션 값을 만들고, 정적 자산과 API 요청 모두 이 값으로 검증합니다(`src/agent_hub/connect_app.py`).
- 요청 본문은 16KB로 제한하고, 정적 자산은 허용 목록에 있는 형식만 내보냅니다(`src/agent_hub/connect_app.py`).
- 로그인 정리 작업이 끝나기 전에 프로세스가 사라지지 않도록 스레드를 데몬으로 두지 않으며, 종료할 때 연결 관리자를 닫습니다(`src/agent_hub/connect_app.py`).
- 이 서비스는 **의도적으로 공개 MCP 표면 밖**에 있습니다. 상태를 바꾸는 동작은 눈에 보이는 로컬 사용자 조작에서 출발해야 하고, 응답에는 가려진 상태만 담깁니다(`src/agent_hub/connect_service.py`).

GUI 뒤의 연결 관리자는 네 provider의 인증·모델 모듈을 직접 연결합니다. Claude는 보안·모델·구독 인증 모듈, Gemini는 인증·계정·동의·모델·OAuth 모듈, Grok은 모델·OAuth·보안 모듈, GPT는 모델·보안 모듈을 사용합니다(`src/agent_hub/connect_service.py`). OAuth 접근 자격, 갱신 자격, 원본 자격 증명은 웹 화면으로 전달되지 않습니다.

### 동의(consent)

각 provider를 호출하기 전에는 로컬 사용 동의가 필요합니다. GUI를 쓸 수 없는 환경에서는 다음
명령을 직접 실행할 수 있습니다.

```bash
./.venv/bin/claude-codex-consent grant --i-understand-and-consent
./.venv/bin/grok-codex-consent grant --i-understand-and-consent
./.venv/bin/google-antigravity-consent grant --i-understand-and-consent
./.venv/bin/openai-codex-consent grant --i-understand-and-consent
```

각 명령의 상태 조회와 해제 옵션은 `--help`에서 확인할 수 있습니다.

### provider별 로그인 소유권

네 provider는 공개 호출 형식은 같지만 인증 내부 구현까지 억지로 같게 만들지 않습니다.

| provider | 로그인 방식 | 자격 증명 소유자 |
| --- | --- | --- |
| Claude | Claude Code 구독 OAuth | Claude Code |
| Grok | xAI device-code OAuth | Agent Hub의 Grok adapter |
| Gemini | Google OAuth PKCE | Agent Hub의 Gemini adapter |
| GPT | 공식 Codex의 ChatGPT 구독 로그인 | Codex |

Claude와 GPT는 각각 Claude Code와 공식 Codex가 로그인 세션을 소유합니다. Agent Hub는 이 세션을
자체 형식으로 복제하거나 브라우저에 노출하지 않습니다. Grok과 Gemini는 Agent Hub가 시작한 OAuth
flow만 완료할 수 있도록 flow ID, 동의 revision, credential revision을 함께 확인합니다.

Claude 보조 스크립트는 상태 확인, 키체인 미러링, 갱신 가능 여부와 안내를 제공합니다.

```bash
claude auth login --claudeai
./.venv/bin/python scripts/claude_codex_login.py status
./.venv/bin/python scripts/claude_codex_login.py mirror-keychain
./.venv/bin/python scripts/claude_codex_login.py refresh
./.venv/bin/python scripts/claude_codex_login.py instructions
```

갱신에 필요한 자격이 없으면 `success=false`와 함께 사유만 돌아오고, 성공한 경우에도 만료 시각만
출력됩니다. 자격 문자열 원문은 표준 출력이나 GUI 응답에 포함하지 않습니다.

Grok과 Gemini의 수동 로그인, 공식 Codex 로그인은 다음과 같습니다.

```bash
./.venv/bin/python scripts/grok_codex_login.py interactive
./.venv/bin/python scripts/google_antigravity_login.py interactive

codex login
# 브라우저를 열기 어려운 환경:
codex login --device-auth
```

---

## 공개 도구 37개

통합 서버는 도구 소유자를 하나로 두고, 각 소유자가 내놓은 도구 명세를 이름 충돌 검사와 함께 단일 목록으로 합칩니다. 이름이 겹치면 서버가 기동에 실패합니다(`src/agent_hub/server.py`). 프롬프트와 리소스는 orchestrate 쪽 MCP 모듈에 위임합니다.

`claude_codex_chat`, `google_antigravity_chat`, `grok_codex_chat`, `openai_codex_chat` 같은 provider별 이름은 **내부 leaf**입니다. 내부 조회에서는 찾을 수 있지만 공개 호출은 알 수 없는 도구로 거부합니다(`tests/agent_hub/test_provider_expansion.py`). 따라서 이 이름들을 클라이언트 설정이나 스킬 문서에 넣지 마세요.

공개 목록은 다음 37개입니다.

| 구분 | 도구 |
| --- | --- |
| 상태 | `agent_hub_status`, `agent_hub_list_models` |
| 연결 안내 | `agent_hub_auth_start`, `agent_hub_auth_complete`, `agent_hub_auth_refresh`, `agent_hub_auth_logout` |
| 대화·생성 | `agent_hub_chat`, `agent_hub_search`, `agent_hub_write`, `agent_hub_generate_image` |
| 검토·릴리스 | `agent_hub_compare_models`, `agent_hub_review_diff`, `agent_hub_release_snapshot`, `agent_hub_release_draft` |
| 설정 | `agent_hub_get_settings`, `agent_hub_update_settings`, `agent_hub_reset_settings` |
| 인계 | `agent_hub_get_handoff`, `agent_hub_prepare_handoff_update`, `agent_hub_apply_handoff_update` |
| workflow 정의 | `agent_hub_list_workflows`, `agent_hub_get_workflow`, `agent_hub_plan_workflow` |
| workflow 실행 | `agent_hub_start_workflow`, `agent_hub_claim_run_action`, `agent_hub_continue_workflow`, `agent_hub_run_workflow` |
| takeover | `agent_hub_prepare_takeover`, `agent_hub_resume_takeover` |
| 실행 조회·관리 | `agent_hub_list_runs`, `agent_hub_get_run`, `agent_hub_get_run_events`, `agent_hub_cancel_run`, `agent_hub_archive_run`, `agent_hub_gc_run` |
| 위임·검증 | `agent_hub_delegate`, `agent_hub_verify` |

자주 사용하는 도구의 입력은 다음과 같습니다.

| 도구 | 하는 일 | 확인된 입력 |
| --- | --- | --- |
| `agent_hub_auth_start` | 로그인 시작 요청, 로컬 GUI 안내 반환 | `provider` |
| `agent_hub_auth_logout` | 로그아웃 요청, 로컬 GUI 안내 반환 | `provider` |
| `agent_hub_list_models` | provider별 모델 목록 조회 | `provider` |
| `agent_hub_get_settings` | provider 설정 조회 | `provider` (기본 `all`) |
| `agent_hub_update_settings` | 명시한 provider의 기본 설정 저장 | 필수 `provider`, 설정 값, `validate` (기본 `true`) |
| `agent_hub_write` | 문서 생성과 품질 게이트 | `provider`, `task`, `source_text` 또는 `instruction`, `project_root`, `quality_rewrite_attempts` |
| `agent_hub_verify` | 작성된 텍스트 검증 | `text`, `project_root`, `doc_class`, `user_facing` |

전체 목록과 각 도구의 정확한 입력 형식은 실행 중인 서버의 도구 목록 응답을 기준으로 확인해 주세요. 이 문서에는 코드와 테스트로 확인한 도구만 적었습니다.

`agent_hub_list_models`의 응답은 provider별로 중첩됩니다. 예를 들어 GPT 항목은 `data.models.gpt.provider == "gpt"` 형태로 돌아옵니다(`tests/agent_hub/test_provider_expansion.py`).

---

## provider 네 개와 지원 기능

registry에는 정확히 네 provider가 순서대로 등록되어 있습니다. `claude`, `grok`, `gemini`, `gpt`입니다(`src/agent_hub/provider_registry.py`). registry 자체에는 인증 로직이나 HTTP 콜백이 없고, 보안 경계는 각 adapter가 가집니다(`src/agent_hub/provider_registry.py`).

### 별칭

익숙한 이름을 그대로 써도 됩니다(`src/agent_hub/provider_registry.py`).

| 입력한 이름 | 실제 provider |
| --- | --- |
| `anthropic` | `claude` |
| `xai` | `grok` |
| `google`, `antigravity`, `google-antigravity` | `gemini` |
| `codex`, `chatgpt`, `openai-codex` | `gpt` |

모델 이름 접두사로도 provider를 추론하며, 판단할 수 없을 때의 기본값은 `gemini`입니다(`src/agent_hub/provider_registry.py`).

### 기능 지원표

아래 값은 registry가 선언한 내용 그대로입니다(`src/agent_hub/provider_registry.py`).

| 기능 | Claude | Grok | Gemini | GPT |
| --- | --- | --- | --- | --- |
| chat | 지원 (추론 강도 low/medium/high) | 지원 | 지원 | 지원 (low/medium/high/xhigh/max/ultra) |
| vision | 지원 | 지원 | 지원 | 지원, 단 원격 이미지 주소는 허용하지 않음 |
| search | 지원 (API 사용 권한 필요) | 지원 (API 사용 권한 필요) | 자체 지원 | **미지원** — 격리 실행되는 내부 GPT leaf는 웹 검색을 끕니다 |
| write | 지원 | 지원 | 지원 | 지원 |
| 이미지 생성 | **미지원** — 텍스트만 반환 | 자체 지원 (사용 권한 필요) | 자체 지원 | **미지원** — 격리 실행은 텍스트만 반환 |
| compare / review_diff / release_draft | 지원 | 지원 | 지원 | 지원 |
| 설정 범위 | model, temperature, max_tokens | model, temperature, max_tokens, api_mode | model, transport, profile, temperature, max_tokens | **model 하나만** |
| 기본 모델 | `claude-sonnet-5` | `grok-4.5` | `gemini-3.5-flash-high` | `gpt-5.6-sol` |
| 계획 수립 참여 | 참여 | 참여 | 참여 | 참여 |
| 기본 비교 대상 | 포함 | 포함 | 포함 | **제외** |

이 중 다음 두 가지 특성은 주의 깊게 확인해 주세요.

- **GPT는 기본 비교 집합에서 빠집니다.** 기본 비교 목록은 `default_compare=True`로 선언한 provider만 모으기 때문입니다(`src/agent_hub/provider_registry.py`). GPT 답변을 비교에 넣으려면 명시적으로 지정해야 합니다.
- **GPT는 설정으로 바꿀 수 있는 값이 모델뿐입니다.** temperature나 max_tokens를 저장하려 하면 지원 범위 밖입니다.

지원하지 않는 기능을 호출하면 실패 메시지(`<provider> does not support <capability>: <이유>`)를 반환합니다(`src/agent_hub/capabilities.py`).

### provider 하나만 쓰기

모든 생성 기능에서 provider를 하나로 지정할 수 있습니다. 예를 들어 `agent_hub_write`에
`provider="claude"`를 넘기면 Claude만 사용합니다. 자동 라우팅이 필요 없는 작업은 이 방식이 가장
단순합니다.

설정 변경은 자동 라우팅 대상이 아닙니다. `agent_hub_update_settings`는 `claude`, `grok`, `gemini`,
`gpt` 중 하나를 필수로 요구하며 `auto`와 provider 생략을 거부합니다. 어느 계정의 설정이 바뀌는지
호출만 보고도 알 수 있도록 만든 경계입니다.

---

## 모델 카탈로그: 실시간 목록과 정적 목록의 차이

모델 조회 기준과 모델 저장 기준은 서로 달라 주의가 필요합니다.

`agent_hub_list_models(probe=true)`와 연결 관리 GUI는 로그인된 provider가 제공하는 live catalog를 먼저
조회합니다. 응답의 catalog source가 `static_fallback`이면 live 조회에 성공한 것이 아니라 패키지에
포함된 안전 목록으로 대체했다는 뜻입니다. 어느 목록에 보이든 현재 계정이 실제 생성 권한을 가졌다는
보장은 아니므로, 중요한 모델은 짧은 실제 호출까지 확인해 주세요.

저장할 때의 검증은 **항상 설치된 정적 카탈로그**를 기준으로 합니다(`src/agent_hub/operations.py`). Claude와 Grok은 패키지에 포함된 선별 목록을 쓰고, Grok에서는 이미지·영상 전용 항목을 제외합니다. Gemini는 정적 모델 카탈로그를 사용하며, GPT는 **기본 모델 하나**만 알려진 텍스트 모델로 취급합니다.

검증은 실패하면 그대로 막아 세웁니다(`src/agent_hub/operations.py`).

- 카탈로그가 비어 있으면 저장하지 않고, 다시 시도하거나 `validate=false`를 명시하라고 알립니다.
- 목록에 없는 모델 ID를 넣으면 설치된 텍스트 모델 카탈로그에 없다고 알리고, provider에서 직접 확인한 뒤에만 `validate=false`를 쓰라고 안내합니다.
- Gemini만 모델 ID를 정규화한 뒤 비교합니다.

`agent_hub_update_settings`의 `validate` 기본값은 `true`입니다(`src/agent_hub/operations.py`). 그래서 **provider가 새로 공개한 모델은 기본 경로에서 거부**되고, 특히 GPT는 정적 집합이 기본 모델 하나뿐이라 다른 모델 ID를 저장하기가 사실상 어렵습니다(`src/agent_hub/operations.py`). 새 모델을 쓰려면 provider 콘솔에서 사용 권한을 확인한 뒤 `validate=false`로 저장하고, 실제 호출로 한 번 확인하는 순서를 권합니다.

설정 조회는 네 provider 모두 `provider`, `defaults`, `overrides`, `selected_model`, `model_source`,
`model_overridden`, `settings_error`, `scope`라는 공통 필드를 반환합니다. Gemini는 transport와 profile을
지원하므로 `model_preferences`, `session`, `profiles` 상세 정보가 추가됩니다. 공통 UI나 자동화는 먼저
공통 필드를 읽고, Gemini 전용 기능이 필요할 때만 상세 필드를 사용하면 됩니다.

---

## 문서 작성과 품질 게이트

`agent_hub_write`는 초안을 만든 다음 문서 품질 검사를 돌립니다. 검사에 걸리면 설정한
`quality_rewrite_attempts`만큼 전체 문서 재작성을 요청하며 기본값과 상한은 모두 `2`입니다. 통과하면
결과에 검사기 버전, 실제 재작성 횟수, `quality_rewrite_applied:<횟수>` 경고를 남깁니다
(`src/agent_hub/operations.py`, `tests/agent_hub/test_provider_expansion.py`).

재작성 후에도 실패하면 **성공으로 감싸지 않습니다.** `success=false`와 `document_quality_failed` 오류를 반환합니다(`tests/agent_hub/test_provider_expansion.py`).

게이트가 잡아내는 대표 사례는 두 가지입니다(`tests/agent_hub/test_provider_expansion.py`).

- 최종 문서에 남은 자리표시자나 미완성 표시 → `placeholder_in_final_document:` 계열 경고
- 저장소에 존재하지 않는 경로 인용 → `repository_path_not_found:` 계열 경고

실제로 존재하는 경로라면 숨김 파일도 통과합니다(`tests/agent_hub/test_provider_expansion.py`).

`agent_hub_verify` 역시 내부 품질 실패를 MCP 성공으로 포장하지 않습니다(`tests/agent_hub/test_provider_expansion.py`). 사용자에게 노출되는 문서를 검증할 때는 `doc_class="durable"`과 `user_facing=true`를 함께 지정해야 경로 검증까지 온전히 적용됩니다.

품질 게이트는 자리표시자, 확인할 수 없는 저장소 경로 같은 구조적 문제를 잡지만 모든 문장의 사실성을
증명하거나 빠진 기능을 찾아내지는 않습니다. 그래서 문서 최종본은 코드와 한 번 더 대조해야 합니다.

---

## workflow: 고정 방식과 적응형 방식

### 고정(fixed) workflow

미리 정의된 작업 절차(recipe)를 실행하는 경로입니다. 구현은 `src/orchestrate_codex/recipes.py`, `src/orchestrate_codex/runner.py`, `src/orchestrate_codex/store.py`에 있습니다. 사용 가능한 recipe 이름은 이 파일들이나 실행 중인 hub의 도구 응답에서 확인해 주세요. 이번 조사에서 코드로 확정하지 못한 recipe 이름은 이 문서에 적지 않았습니다.

### 적응형(adaptive) workflow

모델이 계획을 세우고 hub가 그 계획을 검증한 뒤 실행하는 경로입니다. 계획 형식과 상한은 코드에 고정되어 있습니다(`src/agent_hub/orchestrator.py`).

- 계획 스키마: `agent_hub_plan_v1`
- 최대 단계 수: **12개**
- 단계별 지시문 최대 길이: **4,000자**
- 단계 ID 형식은 정규식으로 제한하고, 의존 관계는 위상 정렬로 처리하며 순환은 오류로 다룹니다.

각 단계 유형은 다음 provider 집합으로 이어집니다(`src/agent_hub/orchestrator.py`).

| 단계 유형 | 실행 주체 |
| --- | --- |
| `chat`, `inspect_codebase`, `review_text` | chat을 지원하는 계획 참여 provider |
| `search`, `write`, `review_diff`, `release_draft` | 해당 기능을 지원한다고 선언한 provider |
| `compare` | 여러 provider |
| `verify`, `release_snapshot` | 로컬 실행 |

`review_text`와 `review_diff`의 구분이 특히 중요합니다.

- **`review_text`**: 아직 파일로 저장하지 않은 초안을 검토하며, chat provider를 그대로 씁니다.
- **`review_diff`**: Git 작업 트리의 변경을 검토하며, registry에서 diff 검토를 선언한 provider로 이어집니다. 네 provider 모두 이 기능을 선언하고 있습니다(`src/agent_hub/provider_registry.py`).

파일에 쓰지 않은 초안을 `review_diff`로 보내면 검토할 변경이 없습니다. 초안 검토는 `review_text`, 저장 후 변경 검토는 `review_diff`로 나눠 주세요.

단계의 주 provider가 실패하고 fallback provider가 성공한 경우, 다음 단계에는 실제 실행 provider와
실패한 시도의 안전한 오류 코드가 함께 전달됩니다. 예를 들어
`actual_provider=gemini; failed_attempts=gpt:codex_process_error`처럼 표시됩니다. 예외 원문, 프롬프트,
자격 문자열, 로컬 경로는 이 실행 이력에 넣지 않습니다.

### 긴 실행과 재개(background continue와 polling)

실행 상태 저장, 재개, 이벤트 기록은 `src/orchestrate_codex/store.py`, `src/orchestrate_codex/events.py`, `src/agent_hub/core/run_lifecycle.py`에 구현되어 있고, `tests/agent_hub/test_run_lifecycle.py`, `tests/agent_hub/test_run_events.py`, `tests/agent_hub/test_run_listing.py`가 회귀를 담당합니다.

여러 wave가 필요한 작업은 먼저 `agent_hub_start_workflow`로 저장합니다. 이후
`agent_hub_continue_workflow`에 `run_id`, 현재 `expected_revision`, `background=true`를 넘기면 revision과
lease를 먼저 확보한 뒤 제한된 background worker에서 다음 wave를 실행합니다. 호출은 즉시 접수 결과를
반환하므로 같은 MCP 서버를 계속 사용할 수 있습니다.

접수 뒤에는 `agent_hub_get_run(run_id=...)`을 polling하세요.

- `lease_active=true`, `continuation_status="running"`이면 같은 run에 continue를 다시 보내지 않습니다.
- `lease_active=false`가 되면 증가한 `store_revision`을 다음 continue의 `expected_revision`으로 사용합니다.
- 예상하지 못한 worker 오류는 원문 대신 `background_worker_failed`로 기록되고, run은 재개 가능한
  `paused` 상태가 됩니다.
- 매우 짧은 wave는 접수 응답보다 먼저 끝날 수 있으므로 최종 상태의 정본은 항상 `agent_hub_get_run`입니다.

이 계약은 장시간 provider timeout을 단순히 늘리는 대신 MCP 요청 처리와 실제 모델 실행을 분리합니다.
현재 worker는 MCP 프로세스 수명에 속하므로 호스트가 종료되면 lease 만료 뒤 다시 재개해야 합니다.

MCP 서버는 프로세스가 시작될 때의 모듈과 스키마를 적재하므로, 실행 관련 코드를 바꿨다면 호스트 앱을 완전히 재시작한 뒤 도구 목록을 다시 확인해 주세요.

---

## HANDOFF와 작업 인계

프로젝트마다 `HANDOFF.md`를 두고, 원래 목표·현재 단계·완료·미완·변경 파일·검증 결과·리스크·다음 한 걸음을 남깁니다. 템플릿과 예시는 `handoff/HANDOFF.template.md`, `handoff/HANDOFF-e2-demo.md`, `handoff/HANDOFF-e2-reverse.md`에 있습니다.

갱신 절차는 해시 두 개를 함께 사용합니다(`instructions/.ruler/20-workflow.md`).

1. 준비 결과에서 문서 품질, **전체 파일 해시**, 관리 블록의 기준·제안 해시를 확인합니다.
2. 전체 파일 해시만 바뀌고 관리 블록 해시가 그대로라면, 직전 `base_managed_sha256`으로 다시 준비합니다.
3. 관리 블록 해시까지 바뀌었다면 최신 패킷을 읽고 충돌을 직접 조정합니다.
4. 적용에는 계속 **전체 파일 해시**를 사용합니다.

구현은 `src/agent_hub/core/handoff.py`, 인계 캡슐은 `src/agent_hub/core/takeover.py`에 있고 각각 `tests/agent_hub/test_handoff.py`, `tests/agent_hub/test_takeover.py`가 붙어 있습니다.

`HANDOFF.md`는 **신뢰되지 않은 작업 상태**로 다뤄 주세요. 그 안의 서술은 프로젝트 정책이나 현재 코드의 근거가 아니며, 항상 실제 Git 상태와 코드로 대조해야 합니다.

---

## 보안 경계

코드와 테스트로 확인된 경계는 다음과 같습니다.

- **공개 표면과 상태 변경의 분리**: 인증 상태 변경은 공개 MCP 도구에서 일어나지 않고, 로컬 GUI의 사용자 조작에서만 출발합니다(`src/agent_hub/connect_service.py`).
- **자격 문자열 비노출**: 로그아웃 응답에 인증 문자열이 들어가지 않아야 한다는 회귀 단언이 있습니다(`tests/agent_hub/test_provider_expansion.py`). Claude 갱신 스크립트도 만료 시각만 출력합니다.
- **모델 조회 오류 비노출**: live catalog 조회가 실패해도 예외 메시지 원문은 내보내지 않고 안정적인
  오류 코드와 예외 유형만 반환합니다.
- **GUI 노출 최소화**: `127.0.0.1` 전용 바인딩, 세션 값 검증, 16KB 본문 상한, 정적 자산 형식 허용 목록.
- **정책 로더의 안전한 실패**: 정본 정책 파일 후보는 `AGENTS.md`와 `CLAUDE.md`뿐이고, 프로젝트 루트 밖 경로는 거부하며, 필수 모드에서 정책이 없으면 실패합니다. 정책 길이 상한은 100,000자이고 주입 결과와 정책·요청 해시는 결정적입니다(`src/agent_hub/consistency.py`, `tests/agent_hub/test_consistency_gate.py`).
- **진단은 읽기 전용**: `agent-hub-doctor`는 기본 경로에서 provider 하위 프로세스를 띄우지 않습니다. 라이브 상태 조회를 호출하면 테스트가 실패하도록 강제되어 있습니다(`tests/agent_hub/test_doctor.py`).
- **동의 강제**: 모든 모델·provider 호출에는 명시적 동의가 필요하고, adapter가 이를 강제합니다(`NOTICE.md`).
- **메모리 격리**: 공유 메모리는 의미 검색을 끈 상태로 로컬 파일에만 기록합니다.

내부 GPT leaf는 registry 선언 수준에서 웹 검색과 이미지 생성을 끄고, 원격 이미지 입력을 허용하지 않습니다(`src/agent_hub/provider_registry.py`).

---

## 저장소 구조

```
agent-hub/
├── src/
│   ├── agent_hub/                 # 통합 서버, 공개 도구, 계획 실행, GUI
│   │   ├── core/                  # 인계, 상한, 경로, 병렬 실행 등 공용 모듈
│   │   ├── connect_ui/            # GUI 정적 자산 (html/css/js)
│   │   └── providers/             # provider adapter와 공개 도구 소유자
│   ├── claude_codex/              # Claude leaf
│   ├── grok_codex/                # Grok leaf
│   ├── google_antigravity_codex/  # Gemini leaf
│   ├── openai_codex/              # GPT leaf
│   └── orchestrate_codex/         # recipe, 상태 저장, 이벤트, 문서 품질 검사
├── hubs/
│   ├── shared/                    # 공유 스킬 정본
│   ├── codex/                     # Codex 플러그인과 스킬
│   └── claude-code/               # Claude Code 플러그인, 커맨드, 스킬
├── plugins/                       # 통합 이전 provider별 레거시 플러그인
├── instructions/                  # 프로젝트 규칙 정본
├── scripts/                       # 설치, 동기화, 진단, 릴리스 스크립트
├── handoff/                       # 인계 템플릿과 예시
├── model-access/                  # 모델 접근 근거 자료
├── memory/                        # 로컬 공유 메모리
├── tests/                         # pytest 스위트
├── .claude-plugin/                # Claude Code 마켓플레이스 메타데이터
└── .agents/                       # 에이전트 플러그인 메타데이터
```

공유 스킬 정본은 `hubs/shared/skills/` 아래 여섯 종(`adaptive-orchestrate`, `document-write`,
`gpt-provider`, `handoff`, `provider-connect`, `takeover`)입니다. `./scripts/sync-hub-skills.sh`가 이
여섯 스킬의 Codex·Claude Code 사본을 동기화합니다. host별 `route-to`까지 포함하면 실제로 노출되는
Agent Hub 스킬은 일곱 종입니다. 예를 들어 인계 스킬 정본은
`hubs/shared/skills/handoff/SKILL.md`입니다.

`plugins/` 아래 네 디렉터리는 통합 이전 구조에서 남은 자료입니다. 지금 권장하는 사용 방식은 **통합 서버 하나만 등록**하는 것이므로, 새로 설치할 때 `plugins/orchestrate-codex/.mcp.json`이나 `plugins/claude-codex/mcp_config.json` 같은 옛 설정을 따라가지 마세요.

과거 standalone Antigravity bundle도 현재 배포 경로가 아닙니다. `scripts/build_plugin_bundle.py`는
조용히 잘못된 번들을 만들지 않고 retired 안내와 `python -m build`, `agent-hub-setup` 경로를
반환합니다.

---

## 문제 해결

**연결 관리 GUI가 열리지 않습니다.**
`agent-hub-connect`는 `127.0.0.1`에만 바인딩하고 세션 값을 요구합니다. 명령이 출력한 주소를 그대로 열어야 하며, 세션 값이 없으면 거부됩니다. 원격 접속이나 다른 기기에서의 접속은 지원 대상이 아닙니다.

**로그인 도구를 불렀는데 `provider_gui_required`가 돌아옵니다.**
정상 동작입니다. 응답의 `next_action.command`에 적힌 `agent-hub-connect` 경로를 직접 실행해 GUI에서 로그인해 주세요.

**모델을 저장하려는데 카탈로그에 없다며 거부됩니다.**
설치된 정적 카탈로그를 기준으로 검증하기 때문입니다. provider에서 그 모델을 실제로 쓸 수 있는지 확인한 뒤 `validate=false`로 저장하고, 곧바로 실제 호출로 확인해 주세요. GPT는 정적 집합이 기본 모델 하나뿐이라 이 상황을 자주 만납니다.

**모델 목록에는 보이는데 호출이 실패합니다.**
로그인 전 목록은 패키지에 포함된 정적 카탈로그일 수 있습니다. 목록에 있다는 사실이 계정 권한을 보장하지는 않으니, GUI에서 연결 상태를 먼저 확인해 주세요.

**Gemini 상태에 `quota_state="unknown"`이 표시됩니다.**
쿼터 소진을 뜻하지 않습니다. 현재 Gemini transport가 쿼터 bucket telemetry를 제공하지 않아
`quota_telemetry_available=false`, `quota_available=null`, `quota_exhausted=null`로 표시한 것입니다.
실제 사용 가능 여부는 GUI의 연결 테스트나 짧은 생성 호출로 확인해 주세요.

**GPT가 비교 결과에 빠져 있습니다.**
GPT는 기본 비교 대상에서 제외되도록 선언되어 있습니다. 필요하면 비교 대상 provider를 명시적으로 지정해 주세요.

**큰 GPT 검토가 `codex_timeout`으로 끝납니다.**
공식 Codex의 `high` reasoning은 큰 dependency context에서 120초보다 오래 걸릴 수 있습니다. 호출자가
지정한 `timeout_sec`가 있으면 그 값이 공통 기본값보다 우선하므로, 검토 범위를 나누거나 reasoning
강도를 낮추거나 timeout을 늘려 주세요. Codex가 내부 `turn.failed`에서 timeout·로그인·구독 문제를
보고하면 Agent Hub는 각각 안정적인 오류 코드로 분류하며, 예외 메시지 원문은 공개 응답에 넣지 않습니다.

**GPT에 웹 검색이나 이미지 생성을 요청했더니 실패합니다.**
격리 실행되는 내부 GPT leaf는 웹 검색을 끄고 텍스트만 반환하도록 registry에 선언되어 있습니다. 이런 작업은 Grok이나 Gemini에 맡기시면 됩니다.

**`agent_hub_write`가 `document_quality_failed`로 끝났습니다.**
자리표시자가 남았거나, 존재하지 않는 저장소 경로를 인용했을 가능성이 큽니다. 경고 메시지의 `placeholder_in_final_document:` 또는 `repository_path_not_found:` 뒤에 붙은 값을 먼저 확인해 주세요.

**로그인 관련 명령이 없다고 나옵니다.**
로그인 보조 스크립트는 실행 명령으로 등록되어 있지 않습니다. `./.venv/bin/python scripts/claude_codex_login.py <하위 명령>` 형태로 실행해 주세요. 파일명에는 하이픈이 아니라 밑줄이 들어갑니다.

**Codex나 Claude Code가 옛 도구 스키마를 씁니다.**
MCP 서버는 프로세스 시작 시점의 모듈과 스키마를 적재합니다. 코드를 바꿨다면 호스트 앱을 완전히 재시작한 뒤 확인해 주세요.

---

## 개발과 검증

```bash
# 테스트 (테스트 경로는 tests, import 경로는 저장소 루트와 src)
./.venv/bin/python -m pytest

# 린트
./.venv/bin/ruff check .
./.venv/bin/ruff check src tests

# 로컬 진단
./.venv/bin/agent-hub-doctor
./scripts/doctor.sh

# 정본 동기화 상태 검사
./scripts/check-sync.sh
./scripts/check-hub-plugins.sh

# 통합 확인 스크립트
./scripts/test-phase1.sh

# 문서 품질 검사 (사용자 노출 문서를 고친 뒤)
./.venv/bin/python -m orchestrate_codex.document_quality README.md

# 릴리스 전 버전 일치 검사
./.venv/bin/python scripts/check_release_version.py

# 배포 패키지 빌드
./.venv/bin/python -m build
```

`scripts/check_release_version.py`는 `pyproject.toml`, `src/agent_hub/__init__.py`, `hubs/codex/.codex-plugin/plugin.json`, `hubs/claude-code/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` 다섯 곳의 버전이 같은지 확인합니다.

`scripts/verify_live_features.py`는 실제 provider를 호출하는 선택적 검사이므로, 로그인과 동의가 끝난 뒤에만 실행해 주세요.

테스트는 영역별로 나뉘어 있습니다. `tests/agent_hub/`는 서버·공개 도구·적응형 실행·연결 GUI·인계·실행 수명주기를 다루고, `tests/google_antigravity_codex/`는 Gemini leaf의 OAuth·스트리밍·근거 첨부·이미지·사용량을 다룹니다. 나머지 디렉터리는 각 leaf와 orchestrate 계층을 담당합니다.

사용자에게 노출되는 한국어 장기 문서를 고쳤다면 `agent_hub_verify`를 `user_facing=true`, `doc_class="durable"`로 실행하고, `python -m orchestrate_codex.document_quality <문서 경로>`, 관련 pytest, `./scripts/check-sync.sh`까지 모두 실행하는 것이 저장소 규칙입니다(`instructions/.ruler/30-documents.md`).

---

## 라이선스

MIT입니다. 자세한 내용은 `LICENSE`와 `NOTICE.md`를 확인해 주세요.
