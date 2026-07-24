# Agent Hub

Claude, Grok, Gemini, GPT를 Codex와 Claude Code에서 같은 방식으로 쓰기 위한 개인용 MCP 서버입니다.

코딩하다 보면 모델 하나만 계속 쓰지는 않게 됩니다. 코드 조사는 Claude에게 맡기고, 다른 모델에게
의견을 물어보거나 검색 결과를 비교할 때도 있습니다. 그런데 모델을 바꿀 때마다 도구 이름과 로그인
방식이 달라지고, 앱을 옮기면 프로젝트 규칙이나 진행 상황도 쉽게 끊깁니다.

Agent Hub는 제가 그 불편을 줄이려고 만든 개인용 도구입니다. 각 서비스의 인증과 요청 형식은 provider
adapter 안에서 처리하고, 사용하는 쪽에는 `agent_hub_*` 도구 37개만 보여 줍니다. Codex에서 시작한
일을 Claude Code에서 이어도 같은 규칙 파일과 인계 기록을 읽습니다.

> 이 프로젝트는 Anthropic, xAI, Google, OpenAI의 공식 제품이 아닙니다.

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

### 로컬 경로 설정

MCP 설정에는 저장소와 가상환경의 절대경로가 들어갑니다. `agent-hub-setup`은 현재 clone 경로를 기준으로
Codex, Claude Code, Cursor, Gemini와 공통 MCP 설정의 변경 계획을 먼저 보여 줍니다. 확인한 뒤에만
`--apply`로 반영하세요. 의존성 설치, provider 로그인, 전역 플러그인 등록, 네트워크 호출은 하지 않습니다.

```bash
./.venv/bin/agent-hub-setup
./.venv/bin/agent-hub-setup --apply
./scripts/sync.sh
./scripts/check-sync.sh
```

### 사용할 모델 로그인

네 provider를 한꺼번에 연결할 필요는 없습니다. 처음에는 자주 쓰는 모델 하나만 설정해도 됩니다. 각 연결은
로컬 사용 동의를 받은 뒤 로그인하도록 되어 있습니다.

터미널 명령 대신 연결 관리 화면에서 네 provider의 동의, 로그인, 연결 테스트와 해제를 한곳에서 처리할 수
있습니다.

```bash
./.venv/bin/agent-hub-connect
```

연결 관리 서버는 `127.0.0.1`에만 열리고, 실행할 때 만든 난수 세션을 브라우저 탭의 `sessionStorage`와
요청 header로 확인합니다. OAuth token은 화면으로 전달하지 않으며, Claude와 GPT 로그인은 각각
Claude Code와 공식 Codex가 계속 관리합니다. Grok과 Gemini는 Agent Hub가 저장한 로컬 로그인 정보만
별도의 확인 뒤 삭제할 수 있습니다.

같은 화면에서 provider별 **Agent Hub 기본 텍스트 모델**도 선택할 수 있습니다. 로그인 전에는 로컬 안전
목록을 보여 주고, 연결된 provider는 첫 조회부터 현재 계정의 최신 모델 목록을 가져옵니다. live 조회가
일시적으로 실패하면 이유와 함께 로컬 목록으로 되돌아갑니다. 저장한 모델은 기본 대화와 문서 작업에
사용되며, 특정 작업에서 모델을 직접 지정하면 그 값이 우선합니다. 기본값으로 복원해도 온도나 출력 길이
같은 다른 provider 설정은 유지됩니다. 실행 중인 adaptive workflow는 시작할 때 선택된 네 provider
모델을 함께 저장하므로, 화면에서 기본 모델을 바꿔도 이미 진행 중인 작업은 같은 모델로 이어집니다.

아래 명령은 GUI를 사용할 수 없는 환경을 위한 수동 방법입니다.

```bash
./.venv/bin/claude-codex-consent grant --i-understand-and-consent
./.venv/bin/grok-codex-consent grant --i-understand-and-consent
./.venv/bin/google-antigravity-consent grant --i-understand-and-consent
./.venv/bin/openai-codex-consent grant --i-understand-and-consent
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

GPT는 별도 MCP나 별도 오케스트레이터를 설치하지 않습니다. 공식 Codex CLI의 ChatGPT 구독 로그인을
그대로 사용합니다.

```bash
codex login
# 브라우저를 열기 어려운 환경에서는:
codex login --device-auth
```

Agent Hub는 Codex OAuth token, Keychain 항목, `auth.json` 내용을 읽거나 복사하지 않습니다. 로그인과
refresh는 공식 Codex가 계속 소유하며, Agent Hub는 redacted 계정 상태와 격리된 `codex exec` 결과만
사용합니다. 설정이 끝나면 `./.venv/bin/agent-hub-doctor`로 로컬 준비 상태를 확인할 수 있습니다.

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
  "handoff_mode": "auto",
  "handoff_search": "nearest",
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
  "expected_revision": 0,
  "max_waves_per_call": 1
}
```

`agent_hub_continue_workflow`는 실행할 수 있는 단계 한 묶음을 처리한 뒤 상태를 다시 저장합니다. 앱이나
MCP 서버가 재시작돼도 같은 `run_id`로 이어갈 수 있습니다. 기본 저장 위치는
`~/.orchestrate_codex/runs/`이며 `ORCHESTRATE_CODEX_STATE_DIR`로 바꿀 수 있습니다.
`expected_revision`에는 직전 응답의 `next_action.arguments.expected_revision`을 사용하세요. 다른 실행이
먼저 상태를 바꿨다면 오래된 결과는 저장하지 않고 revision 충돌로 돌려줍니다.
continue에는 저장된 run의 `run_id`가 항상 필요합니다. 응답의 전체 `state`만 다시 보내는 방식은 revision,
lease, handoff drift를 증명할 수 없어 거부합니다.

실행을 더 진행하지 않을 때는 `agent_hub_cancel_run`에 현재 `store_revision`을 넘깁니다. 취소는 활성
lease를 무효화하고 이후에 도착한 provider 결과를 저장하지 않지만, 이미 시작된 네트워크 요청이나 provider
사용량을 강제로 되돌리지는 않습니다. 종료된 run은 `agent_hub_archive_run`으로 보관 상태로 바꿀 수
있습니다. 실제 상태 JSON 삭제는 `agent_hub_gc_run`의 dry-run 결과에서 revision과 전체 상태 SHA를
확인한 뒤 `apply=true`로 다시 호출해야 합니다. `agent_hub_list_runs`는 같은 프로젝트의 run만 요약해
보여 주며 prompt, 결과 본문, lease token은 반환하지 않습니다.

계획을 만들 때 읽은 `HANDOFF.md`도 실행 상태에 스냅샷으로 보관합니다. 재개 전에 파일이 바뀌면
`handoff_drift`로 멈추므로 새 인계 내용을 검토할 수 있습니다. 변경 전 문맥을 그대로 쓰는 것이
의도라면 `handoff_drift_policy="use-snapshot"`을 명시해야 합니다.

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

`max_leaf_calls`는 provider adapter에 실제로 전달한 논리 호출 수를 셉니다. 기본 비교는 Claude, Grok,
Gemini 세 번을 사용하고, `provider=all`이면 GPT까지 네 번을 사용합니다. 비교 안쪽 호출과 바깥 단계를
합쳐 같은 동시 실행 제한을 공유합니다. HTTP transport 내부의 재시도나 인증 갱신은 별도 leaf call로
세지 않습니다. 동시 실행 제한은 한 workflow 안에서 공유되며, 서로 다른 workflow 프로세스 전체를
묶는 전역 제한은 아닙니다.

## 연결별 지원 범위

| 기능 | Claude | Grok | Gemini | GPT |
|---|:---:|:---:|:---:|:---:|
| 대화와 로컬 이미지 입력 | 지원 | 지원 | 지원 | 지원 |
| 웹 검색 | 지원¹ | 지원¹ | 지원 | 미지원 |
| 문서 작성과 Git 변경 검토 | 지원 | 지원 | 지원 | 지원 |
| 모델 비교와 릴리스 문서 작성 | 지원 | 지원 | 지원 | 지원 |
| 이미지 생성 | 미지원 | 지원¹ | 지원 | 미지원 |

¹ 계정에 해당 API 권한이 있어야 할 수 있습니다.

workflow 계획에서 쓰는 `reasoning_effort`의 공통 값은 `low`, `medium`, `high`입니다.
`agent_hub_chat`이나 `agent_hub_write`로 GPT를 직접 부를 때는 `xhigh`, `max`, `ultra`도 사용할 수
있습니다. 서버는 값을 각 서비스가 이해하는 요청 형식으로 바꾸며, 선택한 provider나 모델이 해당 설정을
지원하지 않으면 조용히 무시하지 않습니다. 사용자가 직접 지정한 값은 오류를 반환하고, workflow가 자동으로
고른 값만 해당 모델에서 사용할 수 없을 때 경고와 함께 생략합니다.

## 공개 도구 37개

모델마다 다른 내부 도구는 밖으로 노출하지 않습니다. 앱에서 보이는 이름은 아래 37개가 전부입니다.

| 구분 | 도구 |
|---|---|
| 상태 확인 | `agent_hub_status`, `agent_hub_list_models` |
| 연결 관리 안내 | `agent_hub_auth_start`, `agent_hub_auth_complete`, `agent_hub_auth_refresh`, `agent_hub_auth_logout` |
| 대화와 생성 | `agent_hub_chat`, `agent_hub_search`, `agent_hub_write`, `agent_hub_generate_image` |
| 검토와 릴리스 | `agent_hub_compare_models`, `agent_hub_review_diff`, `agent_hub_release_snapshot`, `agent_hub_release_draft` |
| 설정 | `agent_hub_get_settings`, `agent_hub_update_settings`, `agent_hub_reset_settings` |
| 인계 기록 | `agent_hub_get_handoff`, `agent_hub_prepare_handoff_update`, `agent_hub_apply_handoff_update` |
| 작업 흐름 | `agent_hub_list_workflows`, `agent_hub_get_workflow`, `agent_hub_plan_workflow`, `agent_hub_start_workflow`, `agent_hub_continue_workflow`, `agent_hub_run_workflow` |
| 실행 제어 | `agent_hub_claim_run_action`, `agent_hub_prepare_takeover`, `agent_hub_resume_takeover`, `agent_hub_list_runs`, `agent_hub_get_run`, `agent_hub_get_run_events`, `agent_hub_cancel_run`, `agent_hub_archive_run`, `agent_hub_gc_run` |
| 위임과 검증 | `agent_hub_delegate`, `agent_hub_verify` |

`agent_hub_release_snapshot`과 `agent_hub_verify`는 모델을 부르지 않고 로컬에서 처리합니다. 이처럼 외부
호출이 필요한 일과 그렇지 않은 일을 구분해 두어서 사용량과 실패 지점을 확인하기 쉽습니다.
`agent_hub_auth_*` 도구는 OAuth code나 token을 MCP로 받지 않고 `agent-hub-connect` 실행 경로만
안내합니다. 실제 동의, 로그인, 다시 로그인과 로컬 로그인 정보 삭제는 연결 관리 화면에서 사용자가 직접
확인해야 합니다. 별도로 실행한 Grok·Google provider MCP의 인증 변경 도구도 같은 화면만 안내합니다.
브라우저를 열 수 없는 환경에서는 위의 consent·login 스크립트를 사용자가 터미널에서 직접 실행하세요.

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
유효하게 답하고, 모든 답이 일치해야 통과합니다. 기본 참여자는 Claude, Grok, Gemini입니다. GPT까지
같은 gate에 넣으려면 `provider=all`을 명시합니다.

응답이 빠지거나 형식이 틀리거나 의견이 갈리면 사람이 확인해야 한다고 표시합니다. 자유롭게 쓴 글의
품질을 숫자 하나로 평가하는 기능은 아닙니다.

프로젝트 규칙을 모델에 전달하면 결과에 `policy_sha256`과 `request_sha256`이 남습니다. 규칙 파일은 작업
방식을 정하는 기준이지, 현재 기능을 증명하는 자료는 아닙니다. 규칙과 코드 설명이 다르면 실제 코드와
테스트에서 확인한 내용이 우선합니다.

## 앱을 바꿔도 이어서 작업하기

Agent Hub는 중요한 문맥을 세 곳에 나눠 둡니다.

- `instructions/.ruler/`에는 사람이 관리하는 프로젝트 규칙이 있습니다. `AGENTS.md`와 `CLAUDE.md`는
  여기서 생성하고, Gemini용 machine-local 설정도 같은 규칙을 가리키게 합니다.
- 각 프로젝트의 `HANDOFF.md`에는 지금 어디까지 했는지, 무엇을 확인했고 다음에 무엇을 해야 하는지
  적습니다. monorepo 하위 프로젝트에 자체 파일이 있으면 그것을 먼저 사용하고, 없을 때만 같은 Git
  저장소 안의 가까운 상위 파일을 찾습니다.
- `memory/data/`에는 나중에 다시 찾을 결정과 교훈을 Markdown으로 보관합니다.

공유 메모리는 `basic-memory`의 로컬 검색을 사용합니다. 기본 설정은 시맨틱 검색을 끄기 때문에 노트 내용을
외부 임베딩 서비스로 보내지 않습니다. 메모리는 검색을 돕는 자료일 뿐, 프로젝트 규칙이나 현재 상태를
대신하지는 않습니다.

인계 기록을 갱신할 때는 먼저 `agent_hub_prepare_handoff_update`로 변경 내용과 현재 파일 SHA를
확인하고, 그 결과를 `agent_hub_apply_handoff_update`에 넘깁니다. prepare는 원래 목표, 현재 단계,
완료, 미완, 변경 파일, 검증, 위험, Do-Not-Repeat와 단 하나의 구체적인 다음 행동이 있는지 검사합니다.
apply는 전체 파일 SHA가 그대로일 때만 marker 블록을 원자적으로 교체합니다.

prepare의 기본 대상은 현재 `project_root`의 `HANDOFF.md`이며, 상위 파일을 의도적으로 갱신하려면
`search=nearest`나 명시적인 `file`을 사용합니다. apply에는 prepare가 반환한 정확한 `target`이
필요합니다. prepare 뒤 marker 밖의 과거 기록만 바뀌었다면 이전 `base_managed_sha256`으로 안전하게
재준비할 수 있습니다. managed SHA도 달라졌다면 최신 패킷을 읽고 충돌을 직접 조정해야 합니다. Git
stage, commit, push는 자동으로 하지 않습니다.

## 보안상 알아둘 점

- OAuth 토큰과 개인 설정은 각 연결의 로컬 설정 디렉터리에 저장합니다. 저장소에 커밋하지 마세요.
- `.env`, SSH 키, 클라우드 자격 증명, 인증서 개인키는 일반 문서나 코드 검토 입력에서 차단합니다.
- `project_root`와 `workspace_root`에는 파일시스템 루트나 홈 디렉터리 전체를 지정할 수 없습니다.
- `HANDOFF.md`는 정책이나 검증된 코드 근거가 아니라 신뢰되지 않은 운영 문맥으로 모델에 전달합니다.
  ignored 파일, symlink, hardlink, 저장소 밖 파일은 읽거나 갱신하지 않습니다.
- GPT adapter는 공식 Codex 로그인만 사용하며 token이나 `auth.json`을 직접 읽지 않습니다. 격리된
  `codex exec`에서 shell, 파일 변경, MCP, web search event가 나타나면 결과를 실패 처리합니다.
- 모델 검색과 이미지 생성은 계정 권한과 사용량을 쓸 수 있습니다. 긴 작업에는 `max_leaf_calls`를 정해
  두는 편이 좋습니다.

고정형 supervised workflow는 provider 호출 결과를 저장할 때 revision과 handoff SHA를 다시
확인합니다. 다만 provider 호출 자체는 호스트 앱이 실행하므로, 동시에 같은 단계를 호출했을 때 발생한
중복 사용량까지 되돌리지는 못합니다.

## 저장소 둘러보기

```text
agent-hub/
├── src/agent_hub/                 # 공개 MCP 도구와 여러 단계 실행
├── src/claude_codex/              # Claude 연결
├── src/grok_codex/                # Grok 연결
├── src/google_antigravity_codex/  # Gemini 연결
├── src/openai_codex/              # 공식 Codex 로그인을 쓰는 GPT 연결
├── src/orchestrate_codex/         # 고정 workflow와 실행 상태 저장
├── model-access/                  # provider 출처와 고정 upstream 경계
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
