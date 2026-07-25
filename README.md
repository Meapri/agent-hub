# Agent Hub

Claude, Grok, Gemini, GPT를 Codex와 Claude Code에서 같은 방식으로 쓰기 위한 개인용
멀티 모델 MCP 서버입니다.

모델을 바꿀 때마다 달라지는 도구 이름과 로그인 절차를 하나로 모으고, 여러 모델이
참여하는 작업을 검증된 순서로 실행합니다. 프로젝트 규칙, `HANDOFF.md`, 로컬 메모리를
공유하므로 Codex에서 시작한 작업을 Claude Code에서 이어 가기도 쉽습니다.

현재 통합 패키지 버전은 **1.4.3**이며 Python 3.10 이상을 지원합니다.

> Agent Hub는 Anthropic, xAI, Google, OpenAI의 공식 제품이 아닙니다.

## 한눈에 보기

- 공개 인터페이스는 provider와 관계없이 `agent_hub_*` 도구 37개로 고정됩니다.
- Claude, Grok, Gemini, GPT를 한 연결 관리 화면에서 로그인하고 모델을 선택할 수 있습니다.
- 짧은 요청은 바로 실행하고, 큰 작업은 고정 workflow나 LLM이 계획한 adaptive workflow로
  나눠 실행합니다.
- 긴 실행은 revision과 lease가 있는 로컬 상태로 저장해 앱이나 MCP 서버가 재시작돼도
  이어 갈 수 있습니다.
- `HANDOFF.md`는 프로젝트마다 두고, SHA로 보호된 prepare/apply 절차로 갱신합니다.
- OAuth token, raw prompt, 계정 식별자는 상태·이벤트·인계 문서에 노출하지 않습니다.

## 설치

프로젝트 규칙 동기화에는 Node.js와 `npx`가 필요합니다. 공유 메모리를 사용하려면
`uvx`도 준비해 주세요.

```bash
git clone https://github.com/Meapri/agent-hub.git
cd agent-hub
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
uv tool install basic-memory
```

실행 패키지만 필요하면 개발 의존성 없이 설치할 수 있습니다.

```bash
./.venv/bin/pip install -e .
```

### 로컬 MCP 경로 설정

MCP 설정에는 clone 위치와 가상환경의 절대경로가 들어갑니다. 먼저 변경 계획을 확인하고,
검토한 뒤 적용하세요.

```bash
./.venv/bin/agent-hub-setup
./.venv/bin/agent-hub-setup --apply
./scripts/sync.sh
./scripts/check-sync.sh
```

`agent-hub-setup`의 기본 실행은 read-only dry-run입니다. `--apply`를 줘야 Codex, Cursor,
Gemini, 공통 MCP, Codex plugin, Claude Code plugin의 로컬 설정에 `agent-hub`와 `memory`
서버를 원자적으로 반영합니다.

의존성 설치, provider 로그인·로그아웃, 전역 plugin 등록, 네트워크 호출은 수행하지 않습니다.

### Codex와 Claude Code에 plugin 등록

`/absolute/path/to/agent-hub`를 실제 clone 경로로 바꿔 주세요.

Codex:

```bash
codex plugin marketplace add /absolute/path/to/agent-hub
codex plugin add agent-hub@agent-hub
codex plugin list
```

Claude Code:

```bash
claude plugin marketplace add /absolute/path/to/agent-hub
claude plugin install agent-hub@agent-hub --scope user
claude plugin list
```

앱을 다시 연 뒤 `agent_hub_status`를 호출하면 사용 동의, 로그인 상태, 기본 모델을 확인할
수 있습니다. `probe=true`는 실제 provider 연결도 확인하므로 네트워크 요청이 발생할 수
있습니다.

## Provider 연결 관리

네 provider를 모두 연결할 필요는 없습니다. 자주 쓰는 연결 하나부터 시작해도 됩니다.

```bash
./.venv/bin/agent-hub-connect
```

연결 관리 서버는 `127.0.0.1`에만 열립니다. 실행할 때 생성한 난수 세션을 브라우저 탭의
`sessionStorage`와 요청 header로 확인하고, OAuth token은 화면으로 전달하지 않습니다.
동의, 로그인, 테스트, 세션 갱신, 다시 로그인, 연결 해제는 사용자가 화면에서 직접
확인해야 실행됩니다.

계정 로그인과 현재 token의 사용 가능 여부는 별도 상태입니다.

| 표시 | 의미 |
|---|---|
| **준비됨** | 지금 모델을 호출할 수 있습니다. |
| **갱신 가능** | 로그인은 유지됐고 저장된 정보로 session을 갱신할 수 있습니다. |
| **로그인됨** | 계정 정보는 있지만 아직 호출 준비가 끝나지 않았습니다. |
| **재로그인 필요** | 저장된 정보만으로 갱신할 수 없어 다시 로그인해야 합니다. |
| **로그인 필요** | 사용할 계정 정보가 없습니다. |

상단의 **상태 다시 확인**은 token을 갱신하거나 생성 요청을 보내지 않습니다. 실제 갱신은
**세션 갱신** 버튼을 눌렀을 때만 시작됩니다. 같은 provider의 중복 갱신은 진행 중인
작업을 재사용하며, 로그인·해제처럼 충돌하는 작업과 늦게 도착한 결과는 revision 검사를
거쳐 차단합니다.

Gemini의 **연결 테스트**는 선택한 모델에 짧은 요청을 보내 실제 응답을 확인하므로 소량의
사용량이 발생할 수 있습니다.

### 터미널에서 직접 연결

GUI를 사용할 수 없는 환경에서는 먼저 각 provider의 로컬 사용 동의를 저장합니다.

```bash
./.venv/bin/claude-codex-consent grant --i-understand-and-consent
./.venv/bin/grok-codex-consent grant --i-understand-and-consent
./.venv/bin/google-antigravity-consent grant --i-understand-and-consent
./.venv/bin/openai-codex-consent grant --i-understand-and-consent
```

Claude는 Claude Code의 구독 OAuth 로그인을 사용합니다.

```bash
claude auth login --claudeai
./.venv/bin/python scripts/claude_codex_login.py mirror-keychain
./.venv/bin/python scripts/claude_codex_login.py status
```

Grok은 브라우저에 표시되는 device code로 로그인합니다.

```bash
./.venv/bin/python scripts/grok_codex_login.py interactive
./.venv/bin/python scripts/grok_codex_login.py status
```

Gemini는 Google Antigravity PKCE 로그인을 사용합니다.

```bash
./.venv/bin/python scripts/google_antigravity_login.py interactive
./.venv/bin/python scripts/google_antigravity_login.py status
```

GPT는 별도 인증 서버를 설치하지 않고 공식 Codex 로그인을 그대로 사용합니다.

```bash
codex login
# 브라우저를 열기 어려운 환경:
codex login --device-auth
```

설정이 끝나면 로컬 준비 상태를 점검하세요.

```bash
./.venv/bin/agent-hub-doctor
```

## 모델 목록과 기본 모델

연결 관리 화면에서 provider별 **Agent Hub 기본 텍스트 모델**을 선택할 수 있습니다.

- 로그인 전에는 코드에 포함된 안전한 fallback 목록을 보여 줍니다.
- 연결된 provider는 첫 조회부터 현재 계정의 live catalog를 요청합니다.
- live 조회가 실패하면 실패 이유와 함께 fallback 목록을 표시합니다.
- 저장한 기본 모델은 대화와 문서 작성에 사용됩니다.
- 호출에서 `model`을 직접 지정하면 저장한 기본 모델보다 우선합니다.
- 모델을 기본값으로 되돌려도 온도나 출력 길이 같은 다른 설정은 유지됩니다.
- adaptive run은 시작할 때 네 provider의 모델을 snapshot으로 저장합니다. 실행 중 GUI에서
  기본 모델을 바꿔도 이미 시작된 run에는 영향을 주지 않습니다.

`agent_hub_list_models`의 `source`를 확인하면 live 응답과 `static_fallback`을 구분할 수
있습니다. fallback 목록을 현재 계정에서 실제로 조회한 결과로 해석하면 안 됩니다.

설정은 `agent_hub_get_settings`, `agent_hub_update_settings`,
`agent_hub_reset_settings`로도 관리할 수 있습니다. `provider=auto`는 요청한 기능과 모델
설정을 기준으로 연결을 선택합니다.

## 빠른 사용법

한 번의 호출로 끝나는 질문이나 문서는 `agent_hub_chat`, `agent_hub_write`에 바로 맡기면
됩니다. 저장소 조사와 작성처럼 순서가 있는 작업은 workflow를 사용하세요.

## 공개 도구 37개

Agent Hub가 공개하는 도구는 다음 37개입니다.

| 구분 | 도구 |
|---|---|
| 상태 | `agent_hub_status`, `agent_hub_list_models` |
| 연결 안내 | `agent_hub_auth_start`, `agent_hub_auth_complete`, `agent_hub_auth_refresh`, `agent_hub_auth_logout` |
| 대화·생성 | `agent_hub_chat`, `agent_hub_search`, `agent_hub_write`, `agent_hub_generate_image` |
| 검토·릴리스 | `agent_hub_compare_models`, `agent_hub_review_diff`, `agent_hub_release_snapshot`, `agent_hub_release_draft` |
| 설정 | `agent_hub_get_settings`, `agent_hub_update_settings`, `agent_hub_reset_settings` |
| 인계 | `agent_hub_get_handoff`, `agent_hub_prepare_handoff_update`, `agent_hub_apply_handoff_update` |
| Workflow | `agent_hub_list_workflows`, `agent_hub_get_workflow`, `agent_hub_plan_workflow`, `agent_hub_start_workflow`, `agent_hub_continue_workflow`, `agent_hub_run_workflow` |
| 실행 제어 | `agent_hub_claim_run_action`, `agent_hub_prepare_takeover`, `agent_hub_resume_takeover`, `agent_hub_list_runs`, `agent_hub_get_run`, `agent_hub_get_run_events`, `agent_hub_cancel_run`, `agent_hub_archive_run`, `agent_hub_gc_run` |
| 위임·검증 | `agent_hub_delegate`, `agent_hub_verify` |

`agent_hub_auth_*`는 OAuth code나 token을 MCP 인자로 받지 않는 read-only 안내 도구입니다.
실제 인증 변경은 연결 관리 화면이나 위의 수동 명령에서만 진행합니다.

### Provider별 지원 범위

| 기능 | Claude | Grok | Gemini | GPT |
|---|:---:|:---:|:---:|:---:|
| 대화·로컬 이미지 입력 | 지원 | 지원 | 지원 | 지원 |
| 웹 검색 | 지원¹ | 지원¹ | 지원 | 미지원 |
| 문서 작성·Git diff 검토 | 지원 | 지원 | 지원 | 지원 |
| 모델 비교·릴리스 문서 | 지원 | 지원 | 지원 | 지원 |
| 이미지 생성 | 미지원 | 지원¹ | 지원 | 미지원 |

¹ 계정에 해당 API 권한이 있어야 할 수 있습니다.

## Workflow

### 준비된 workflow

| 이름 | 용도 | Preset |
|---|---|---|
| `repo_document` | 저장소를 조사해 README나 기술 문서를 작성 | `readme`, `technical-doc`, `proposal` |
| `git_document` | 현재 Git 변경으로 PR 설명이나 릴리스 노트를 작성 | `pr-description`, `release-notes` |
| `research_brief` | 근거가 있는 웹 검색 결과를 짧은 조사 문서로 정리 | `default` |
| `deep_readme` | 여러 모델이 구조와 사용법을 나눠 조사한 뒤 README 작성 | `default` |

`agent_hub_list_workflows`에는 위 네 고정 workflow와 동적 `adaptive` workflow가 표시됩니다.

### Adaptive workflow

순서를 미리 정하기 어려운 작업은 `workflow_id="adaptive"`로 계획합니다. planner가
`agent_hub_plan_v1` DAG를 만들고, 로컬 validator가 다음을 확인합니다.

- 최대 12단계와 provider 최대 호출 수
- 지원 가능한 capability와 provider 조합
- 순환 의존성, 고립된 단계, 여러 final 단계
- 서로 독립적인 단계만 같은 wave에서 병렬 실행
- 선언된 fallback과 문서 품질 재작성 횟수

저장소 조사 단계는 `investigation_depth`를 `shallow`, `standard`, `deep` 중에서 선택할
수 있습니다. 먼저 `agent_hub_plan_workflow`로 계획을 검토한 뒤 실행하는 방법을 권장합니다.

```json
{
  "workflow_id": "adaptive",
  "prompt": "코드와 테스트를 깊게 조사한 뒤 README를 작성해 줘",
  "project_root": "/absolute/path/to/repository",
  "policy_mode": "required",
  "handoff_mode": "auto",
  "handoff_search": "nearest",
  "max_steps": 6,
  "max_leaf_calls": 12
}
```

짧은 계획은 `agent_hub_run_workflow`로 한 번에 실행할 수 있습니다. 오래 걸리거나 wave가
여러 개인 계획은 `agent_hub_start_workflow`와 `agent_hub_continue_workflow`를 사용하세요.

```json
{
  "run_id": "<start가 반환한 run_id>",
  "expected_revision": 1,
  "max_waves_per_call": 1
}
```

`expected_revision`에는 직전 응답의 `next_action.arguments.expected_revision`을 그대로
사용해야 합니다. 응답의 `state`만 다시 보내는 방식은 revision, lease, handoff drift를
증명할 수 없어 거부합니다.

### 긴 실행과 실패 복구

장문 조사와 문서 작성이 중간에 끊기지 않도록 기본 제한을 넉넉하게 잡습니다.

| 환경변수 | 기본값 | 역할 |
|---|---:|---|
| `AGENT_HUB_MCP_CALL_TIMEOUT` | `1800` | MCP 호출 전체 제한 |
| `AGENT_HUB_TIMEOUT_RETURN_MARGIN` | `10` | 상태를 저장하고 응답할 여유 |
| `AGENT_HUB_WORKFLOW_TIMEOUT` | `1740` | 한 번의 adaptive 실행·재개 제한 |
| `AGENT_HUB_PER_CALL_TIMEOUT` | `900` | provider 한 번의 호출 제한 |

환경변수는 MCP 서버를 시작할 때 읽습니다. 값을 바꾼 뒤에는 Codex나 Claude Code의 Agent Hub
MCP를 다시 시작해야 합니다.

workflow 제한은 MCP 제한에서 반환 여유를 뺀 값을 넘을 수 없으며, provider 호출도 남은
workflow 시간 안으로 자동 조정됩니다. planner가 잘못된 JSON을 반환하면 기본 3회, 최대
5회까지 validator 오류를 포함해 교정을 요청합니다.

전체 workflow 시간이 끝나거나 provider가 `TimeoutError`, `codex_timeout` 같은 timeout을
반환하면 완료된 단계는 버리지 않습니다. run을 `paused`로 저장하고 `resumable=true`,
`run_id`, 다음 revision을 반환합니다. provider timeout은 redacted
`provider_call_timeout`으로 정규화되며, `continue`에서 끝나지 않은 단계만 다시 실행합니다.

`agent_hub_cancel_run`은 활성 lease를 무효화해 늦게 도착한 결과가 상태를 덮어쓰지 못하게
합니다. 이미 시작된 네트워크 요청이나 발생한 사용량까지 되돌리지는 않습니다.

## `HANDOFF.md`와 takeover

프로젝트별 작업 상태는 해당 프로젝트 루트의 `HANDOFF.md`에 둡니다. monorepo 하위
프로젝트에 자체 파일이 있으면 그 파일을 우선하고, 없을 때만 같은 Git 저장소 안의 가까운
상위 파일을 찾습니다. 다른 저장소나 형제 프로젝트의 인계 기록은 사용하지 않습니다.

`HANDOFF.md`는 정책이나 현재 코드의 증거가 아니라 **신뢰되지 않은 운영 문맥**입니다.
workflow는 시작할 때 파일과 SHA를 snapshot으로 저장하고, 재개 시 파일이 바뀌었으면
`handoff_drift`로 멈춥니다. 이전 snapshot을 의도적으로 사용할 때만
`handoff_drift_policy="use-snapshot"`을 명시하세요.

갱신은 두 단계로 진행합니다.

1. `agent_hub_prepare_handoff_update`가 필수 항목, 문서 품질, 전체 파일 SHA,
   managed block SHA를 검사합니다.
2. `agent_hub_apply_handoff_update`가 prepare에서 받은 전체 파일 SHA가 그대로일 때만
   marker block을 원자적으로 교체합니다.

전체 파일 SHA만 충돌하고 managed SHA가 그대로라면 직전 `base_managed_sha256`으로 다시
prepare할 수 있습니다. managed SHA도 달라졌다면 최신 packet을 읽고 충돌을 직접 조정해야
합니다.

다른 harness가 실행 중인 run을 이어받을 때는 `agent_hub_prepare_takeover`로 capsule을
만들고 `agent_hub_resume_takeover`로 revision·lease·프로젝트 경계를 다시 검증합니다.
takeover나 handoff는 Git stage, commit, push를 자동으로 실행하지 않습니다.

## 보안과 개인정보

- Claude와 GPT 인증은 각각 Claude Code와 공식 Codex가 소유합니다. Agent Hub가 공동
  로그인을 삭제하지 않습니다.
- Grok과 Gemini는 Agent Hub가 저장한 로컬 인증 정보만 별도 확인 뒤 삭제할 수 있습니다.
- GPT adapter는 Codex OAuth token, Keychain 항목, `auth.json`을 읽거나 복사하지 않습니다.
  공식 `codex exec`의 redacted 상태와 격리된 텍스트 결과만 사용합니다.
- GPT token refresh도 공식 Codex가 소유하므로 GUI에서 `refresh_supported=false`로
  표시됩니다.
- 격리된 GPT 실행에서 shell, 파일 변경, MCP, web search event가 나타나면 결과를
  실패 처리합니다.
- `.env`, SSH key, cloud credential, 인증서 private key는 일반 문서·diff 입력에서
  차단합니다.
- `project_root`와 `workspace_root`에는 파일시스템 루트나 홈 디렉터리 전체를 지정할 수
  없습니다.
- token, raw prompt, 계정 식별자, credential 경로는 job·HTTP·DOM·event·handoff에
  기록하지 않습니다.
- 검색, 이미지 생성, 연결 테스트, 긴 workflow는 provider 사용량을 발생시킬 수 있습니다.

공유 메모리는 `basic-memory`의 로컬 Markdown 저장소를 사용합니다. 기본 설정은 semantic
search를 끄므로 노트 내용을 외부 embedding 서비스로 보내지 않습니다. 메모리는 검색을
돕는 자료이며 프로젝트 규칙이나 현재 상태를 대신하지 않습니다.

## 문제 해결

### 연결 상태가 이상할 때

```bash
./.venv/bin/agent-hub-doctor
./.venv/bin/agent-hub-connect
```

상태 조회는 read-only입니다. **갱신 가능**이면 세션 갱신을, **재로그인 필요**이면 다시
로그인을 선택하세요. 네트워크 실패만으로 재로그인이 필요하다고 단정하지 않습니다.

### 모델 목록이 일부만 보일 때

`agent_hub_list_models`의 `source`, `fallback_reason`, 인증 상태를 함께 확인하세요.
`static_fallback`이면 live catalog가 아닙니다. 로그인과 연결 테스트가 끝난 뒤 다시
조회해도 계속 fallback이면 provider 응답과 local server log를 확인하세요.

### 변경한 코드나 timeout이 반영되지 않을 때

editable install을 사용해도 실행 중인 MCP 프로세스는 이미 import한 모듈을 유지합니다.
Codex나 Claude Code에서 Agent Hub MCP를 재시작한 뒤 `agent_hub_status`와 tool schema를
다시 확인하세요.

### Plugin이나 생성 파일이 어긋날 때

```bash
./scripts/sync.sh
./scripts/check-sync.sh
./scripts/check-hub-plugins.sh
```

`CLAUDE.md`, `AGENTS.md`, machine-local MCP 설정은 생성 결과입니다. 규칙은
`instructions/.ruler/`에서 수정하고 다시 동기화하세요.

## 저장소 구조

```text
agent-hub/
├── src/agent_hub/                 # 공개 MCP 도구, 연결 UI, provider 통합
├── src/orchestrate_codex/         # 고정 workflow와 실행 상태
├── src/claude_codex/              # Claude 연결
├── src/grok_codex/                # Grok 연결
├── src/google_antigravity_codex/  # Gemini 연결
├── src/openai_codex/              # 공식 Codex 로그인을 쓰는 GPT 연결
├── hubs/codex/                    # Codex plugin
├── hubs/claude-code/              # Claude Code plugin
├── instructions/.ruler/           # 프로젝트 규칙 원본
├── model-access/                  # provider 출처와 고정 upstream 경계
├── memory/                        # 공유 로컬 메모리
├── scripts/                       # 설정, 동기화, 검증 도구
└── tests/                         # 단위·계약·통합 테스트
```

## 개발과 검증

```bash
./.venv/bin/ruff check src tests
./.venv/bin/pytest -q
./scripts/check-hub-plugins.sh
./scripts/check-sync.sh
./scripts/test-phase1.sh
./scripts/doctor.sh
./.venv/bin/python -m orchestrate_codex.document_quality README.md
./.venv/bin/python scripts/check_release_version.py
./.venv/bin/python -m build
git diff --check
```

사용자용 문서는 `agent_hub_verify`에 `doc_class="durable"`,
`user_facing=true`, 현재 `project_root`를 넘겨 추가 검증합니다. 이 검사는 외부 모델을
호출하지 않고 문서 완결성, 경로·명령 근거, 동기화 상태를 결정적으로 확인합니다.

## 현재 제한

- provider 최대 호출 수는 `max_leaf_calls`로 제한됩니다. 비교 안쪽 호출과 바깥 workflow
  단계가 같은 예산을 사용합니다.
- 동시 실행 제한은 한 workflow 안에서만 공유되며, 여러 workflow 프로세스를 묶는 전역
  제한은 없습니다.
- `agent_hub_cancel_run`은 상태 저장을 막지만 이미 시작된 원격 요청이나 사용량을
  되돌리지 못합니다.
- GPT는 격리 정책 때문에 Agent Hub 내부 web search와 image generation을 지원하지
  않습니다.
- 검색과 이미지 생성은 계정의 API 권한에 따라 사용할 수 없을 수 있습니다.
- 문서 품질 gate는 잘린 문장, 미완성 표시, 설명 없는 내부 용어, 확인되지 않은 경로를
  잡지만 사실 확인을 대신하지 않습니다. 최종 문서는 코드·테스트와 함께 검토해야 합니다.
