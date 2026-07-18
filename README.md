# Agent Hub

Claude, Grok, Gemini를 Codex와 Claude Code에서 같은 방식으로 쓰기 위한 개인용 MCP 서버입니다.

코딩하다 보면 모델 하나만 계속 쓰지는 않게 됩니다. 코드 조사는 Claude에게 맡기고, 다른 모델에게
의견을 물어보거나 검색 결과를 비교할 때도 있습니다. 그런데 모델을 바꿀 때마다 도구 이름과 로그인
방식이 달라지고, 앱을 옮기면 프로젝트 규칙이나 진행 상황도 쉽게 끊깁니다.

Agent Hub는 제가 그 불편을 줄이려고 만든 개인용 도구입니다. 각 서비스의 인증과 요청 형식은 서버 안에서 처리하고,
사용하는 쪽에는 `agent_hub_*` 도구 26개만 보여 줍니다. Codex에서 시작한 일을 Claude Code에서 이어도
같은 규칙 파일과 인계 기록을 읽습니다.

> 이 프로젝트는 Anthropic, xAI, Google의 공식 제품이 아닙니다.

## 바로 설치하기

Python 3.10 이상이 필요합니다. 프로젝트 규칙을 동기화하려면 Node.js와 `npx`가 있어야 하고, 공유
메모리까지 쓰려면 `uvx`도 준비해 주세요.

```bash
git clone https://github.com/Meapri/agent-hub.git
cd agent-hub
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
uv tool install basic-memory
```

실행에 필요한 패키지만 설치하려면 `./.venv/bin/pip install -e .`로 충분합니다. `.[dev]`에는 테스트,
Ruff, 빌드 도구가 함께 들어 있습니다.

### 로컬 경로 확인

MCP 설정에는 저장소와 가상환경의 절대경로가 들어갑니다. 다른 위치에 클론했다면 아래 세 파일에서
`agent-hub-mcp`, `BASIC_MEMORY_CONFIG_DIR`, `BASIC_MEMORY_HOME` 경로를 먼저 고쳐 주세요.

- `instructions/.ruler/ruler.toml`
- `hubs/codex/.mcp.json`
- `hubs/claude-code/.mcp.json`

그다음 프로젝트 규칙을 각 앱의 설정 파일로 동기화합니다.

```bash
./scripts/sync.sh
./scripts/check-sync.sh
```

### 사용할 모델 로그인

세 모델을 한꺼번에 연결할 필요는 없습니다. 처음에는 자주 쓰는 모델 하나만 설정해도 됩니다. 각 연결은
로컬 사용 동의를 받은 뒤 로그인하도록 되어 있습니다.

```bash
./.venv/bin/claude-codex-consent grant --i-understand-and-consent
./.venv/bin/grok-codex-consent grant --i-understand-and-consent
./.venv/bin/google-antigravity-consent grant --i-understand-and-consent
```

Claude는 Claude Code의 구독 OAuth 정보를 연결합니다.

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

Gemini 연결에는 Google Antigravity의 PKCE 로그인을 사용합니다.

```bash
./.venv/bin/python scripts/google_antigravity_login.py interactive
./.venv/bin/python scripts/google_antigravity_login.py status
```

설정이 끝나면 `./scripts/doctor.sh`로 로컬 준비 상태를 확인할 수 있습니다.

### Codex 또는 Claude Code에 추가

아래 명령의 `/absolute/path/to/agent-hub`를 실제 클론 경로로 바꿔 주세요.

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

플러그인은 MCP 서버 두 개를 등록합니다. `agent-hub`는 모델 호출과 여러 단계의 작업을 맡고, `memory`는
Git으로 관리하는 로컬 메모리를 제공합니다. 앱을 다시 연 뒤 `agent_hub_status`를 호출해 동의 여부,
로그인 상태, 기본 모델을 확인해 보세요. `probe=true`를 주면 실제 연결까지 점검합니다.

## 평소에는 이렇게 씁니다

짧은 질문이나 문서 한 문단처럼 한 번의 호출로 끝나는 일은 `agent_hub_chat`이나 `agent_hub_write`에 바로
맡기면 됩니다. `provider=auto`를 쓰면 요청한 기능과 모델 설정을 보고 알맞은 연결을 고릅니다.

조금 큰 일은 작업 흐름(workflow)으로 실행할 수 있습니다. 예를 들어 README를 새로 쓰는 작업이라면
저장소를 조사하는 단계와 실제 작성 단계를 나누고, 조사 결과가 준비된 뒤에만 글을 쓰게 할 수 있습니다.

이미 준비된 작업 흐름은 네 가지입니다.

| 이름 | 쓸 만한 상황 | 선택할 수 있는 preset |
|---|---|---|
| `repo_document` | 저장소를 읽고 README나 기술 문서를 쓸 때 | `readme`, `technical-doc`, `proposal` |
| `git_document` | 현재 Git 변경으로 PR 설명이나 릴리스 노트를 만들 때 | `pr-description`, `release-notes` |
| `research_brief` | 웹 검색을 거쳐 짧은 조사 문서를 만들 때 | `default` |
| `deep_readme` | 여러 모델이 구조와 사용법을 나눠 확인해야 할 때 | `default` |

순서를 미리 정하기 어려운 일에는 `workflow_id="adaptive"`를 사용합니다. 계획 모델이 필요한 단계를 만들고,
Agent Hub가 잘못된 의존 관계나 과도한 호출 수가 없는지 확인한 뒤 실행합니다.

```json
{
  "workflow_id": "adaptive",
  "prompt": "코드와 테스트를 조사해 설치 가이드를 작성해 줘",
  "project_root": "/absolute/path/to/repository",
  "policy_mode": "required",
  "max_steps": 6,
  "max_leaf_calls": 12
}
```

먼저 `agent_hub_plan_workflow`로 계획만 받아 보는 편이 좋습니다. 조사 단계가 작성 단계보다 앞에 있는지,
서로 상관없는 작업만 동시에 실행되는지 확인해 주세요. 짧은 계획은 `agent_hub_run_workflow`로 한 번에
실행할 수 있습니다.

오래 걸릴 만한 계획은 `agent_hub_start_workflow`로 시작하세요. 반환된 `run_id`를 보관해 두고 다음 호출을
반복하면 됩니다.

```json
{
  "run_id": "<start가 반환한 run_id>",
  "max_waves_per_call": 1
}
```

`agent_hub_continue_workflow`는 실행할 수 있는 단계 한 묶음을 처리한 뒤 상태를 다시 저장합니다. 앱이나
MCP 서버가 재시작돼도 같은 `run_id`로 이어갈 수 있습니다. 기본 저장 위치는
`~/.orchestrate_codex/runs/`이며 `ORCHESTRATE_CODEX_STATE_DIR`로 바꿀 수 있습니다.

## 시간이 오래 걸리는 작업

모델 두세 개가 긴 코드를 읽으면 한 번의 MCP 호출 제한을 넘길 수 있습니다. Agent Hub는 기본 300초 제한
안에서 응답을 돌려주기 위해 10초를 남겨 두고, 한 번의 실행에는 270초를 사용합니다.

| 환경변수 | 기본값 | 무엇을 바꾸나요? |
|---|---:|---|
| `AGENT_HUB_MCP_CALL_TIMEOUT` | `300` | MCP 클라이언트의 실제 호출 제한 |
| `AGENT_HUB_TIMEOUT_RETURN_MARGIN` | `10` | 실행 상태를 저장하고 응답할 여유 시간 |
| `AGENT_HUB_WORKFLOW_TIMEOUT` | `270` | 한 번의 실행이나 재개 호출에 쓸 시간 |

시간이 다 됐다고 완료된 앞 단계를 버리지는 않습니다. `start`와 `continue`로 시작한 실행은 `paused`로
저장됩니다. `run_workflow`가 `timed_out`으로 끝나더라도 `resumable=true`와 `run_id`가 함께 왔다면 그
실행을 이어갈 수 있습니다.

중간 단계에서 나온 글은 최종 결과로 저장하지 마세요. `status=completed`인지, 마지막 품질 검사까지
통과했는지 확인한 뒤 파일에 반영하는 것이 안전합니다.

## 연결별 지원 범위

| 기능 | Claude | Grok | Gemini |
|---|:---:|:---:|:---:|
| 대화와 이미지 입력 | 지원 | 지원 | 지원 |
| 웹 검색 | 지원¹ | 지원¹ | 지원 |
| 문서 작성과 Git 변경 검토 | 지원 | 지원 | 지원 |
| 모델 비교와 릴리스 문서 작성 | 지원 | 지원 | 지원 |
| 이미지 생성 | 미지원 | 지원¹ | 지원 |

¹ 계정에 해당 API 권한이 있어야 할 수 있습니다.

`reasoning_effort`는 `low`, `medium`, `high` 중 하나를 고를 수 있습니다. 서버는 이 값을 각 서비스가
이해하는 요청 형식으로 바꿉니다. 선택한 모델이 해당 설정을 지원하지 않으면 조용히 무시하지 않고 오류를
반환합니다.

## 공개 도구 26개

모델마다 다른 내부 도구는 밖으로 노출하지 않습니다. 앱에서 보이는 이름은 아래 26개가 전부입니다.

| 구분 | 도구 |
|---|---|
| 상태 확인 | `agent_hub_status`, `agent_hub_list_models` |
| 로그인 | `agent_hub_auth_start`, `agent_hub_auth_complete`, `agent_hub_auth_refresh`, `agent_hub_auth_logout` |
| 대화와 생성 | `agent_hub_chat`, `agent_hub_search`, `agent_hub_write`, `agent_hub_generate_image` |
| 검토와 릴리스 | `agent_hub_compare_models`, `agent_hub_review_diff`, `agent_hub_release_snapshot`, `agent_hub_release_draft` |
| 설정 | `agent_hub_get_settings`, `agent_hub_update_settings`, `agent_hub_reset_settings` |
| 작업 흐름 | `agent_hub_list_workflows`, `agent_hub_get_workflow`, `agent_hub_plan_workflow`, `agent_hub_start_workflow`, `agent_hub_continue_workflow`, `agent_hub_get_run`, `agent_hub_run_workflow` |
| 위임과 검증 | `agent_hub_delegate`, `agent_hub_verify` |

`agent_hub_release_snapshot`과 `agent_hub_verify`는 모델을 부르지 않고 로컬에서 처리합니다. 이처럼 외부
호출이 필요한 일과 그렇지 않은 일을 구분해 두어서 사용량과 실패 지점을 확인하기 쉽습니다.

## 문서를 쓸 때 지키는 것

README나 오래 보관할 기술 문서는 모델이 그럴듯하게 썼다는 이유만으로 통과시키지 않습니다.
`agent_hub_write`는 완성된 글에서 아래 문제를 찾습니다.

- 쓰다 만 문서나 중간에서 잘린 문장
- 번역한 티가 나는 표현과 설명 없이 등장한 내부 용어
- 나중에 채우겠다는 뜻으로 남겨 둔 미완성 표시
- 실제 저장소에 없는 소스 파일을 있는 것처럼 적은 문장

검사에 걸리면 기본적으로 문서 전체를 한 번 다시 씁니다. 그래도 문제가 남으면 성공으로 포장하지 않고
`document_quality_failed`를 반환합니다. 검사 결과는 `quality_gate`에서 확인할 수 있습니다.

README를 직접 작성 도구에 맡길 때는 `project_root`, `policy_mode=required`, `task=readme`를 함께 넘겨
주세요. 상대경로로 지정한 `source_file`은 MCP 서버의 실행 위치가 아니라 `project_root` 또는
`workspace_root` 아래에서 찾습니다.

## 모델 의견을 비교할 때

`agent_hub_compare_models`의 Consistency Gate는 승인과 거절처럼 답의 선택지가 정해진 판단에 씁니다.
각 모델은 허용된 답 중 하나를 정해진 JSON 형식으로 반환해야 합니다. 기본 설정에서는 모델이 둘 이상
유효하게 답하고, 모든 답이 일치해야 통과합니다.

응답이 빠지거나 형식이 틀리거나 의견이 갈리면 사람이 확인해야 한다고 표시합니다. 자유롭게 쓴 글의
품질을 숫자 하나로 평가하는 기능은 아닙니다.

프로젝트 규칙을 모델에 전달하면 결과에 `policy_sha256`과 `request_sha256`이 남습니다. 규칙 파일은 작업
방식을 정하는 기준이지, 현재 기능을 증명하는 자료는 아닙니다. 규칙과 코드 설명이 다르면 실제 코드와
테스트에서 확인한 내용이 우선합니다.

## 앱을 바꿔도 이어서 작업하기

Agent Hub는 중요한 문맥을 세 곳에 나눠 둡니다.

- `instructions/.ruler/`에는 사람이 관리하는 프로젝트 규칙이 있습니다. `AGENTS.md`, `CLAUDE.md`,
  `.gemini/settings.json`은 여기서 생성합니다.
- `HANDOFF.md`에는 지금 어디까지 했는지, 무엇을 확인했고 다음에 무엇을 해야 하는지 적습니다.
- `memory/data/`에는 나중에 다시 찾을 결정과 교훈을 Markdown으로 보관합니다.

공유 메모리는 `basic-memory`의 로컬 검색을 사용합니다. 기본 설정은 시맨틱 검색을 끄기 때문에 노트 내용을
외부 임베딩 서비스로 보내지 않습니다. 메모리는 검색을 돕는 자료일 뿐, 프로젝트 규칙이나 현재 상태를
대신하지는 않습니다.

## 보안상 알아둘 점

- OAuth 토큰과 개인 설정은 각 연결의 로컬 설정 디렉터리에 저장합니다. 저장소에 커밋하지 마세요.
- `.env`, SSH 키, 클라우드 자격 증명, 인증서 개인키는 일반 문서나 코드 검토 입력에서 차단합니다.
- `project_root`와 `workspace_root`에는 파일시스템 루트나 홈 디렉터리 전체를 지정할 수 없습니다.
- 모델 검색과 이미지 생성은 계정 권한과 사용량을 쓸 수 있습니다. 긴 작업에는 `max_leaf_calls`를 정해
  두는 편이 좋습니다.

## 저장소 둘러보기

```text
agent-hub/
├── src/agent_hub/                 # 공개 MCP 도구와 여러 단계 실행
├── src/claude_codex/              # Claude 연결
├── src/grok_codex/                # Grok 연결
├── src/google_antigravity_codex/  # Gemini 연결
├── src/orchestrate_codex/         # 고정 workflow와 실행 상태 저장
├── hubs/codex/                    # Codex 플러그인
├── hubs/claude-code/              # Claude Code 플러그인
├── instructions/.ruler/           # 프로젝트 규칙 원본
├── memory/                        # 공유 로컬 메모리
├── scripts/                       # 설치 확인, 동기화, 수용 검사
└── tests/                         # 단위·계약·통합 테스트
```

## 개발과 검증

변경을 마쳤다면 아래 검사를 순서대로 실행해 주세요.

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

`README.md`, `RUN-REPORT.md`, `HANDOFF.md`도 코드와 함께 관리합니다. 테스트가 모두 통과했더라도 이 문서가
현재 동작과 다르면 아직 작업이 끝난 것이 아닙니다.
