# Agent Hub

Agent Hub는 Claude, Grok, Gemini, GPT를 한곳에서 연결하고 작업에 맞게 조합하는
macOS용 멀티 모델 실행 환경입니다.

여러 모델을 호출하는 데서 끝나지 않습니다. 작업 계획, provider 선택, 실행 상태, 결과물과
인계 기록을 로컬에서 함께 관리합니다. Codex나 Claude Code가 종료되어도 실행 상태가 남기
때문에 긴 작업을 다시 이어갈 수 있습니다.

- 현재 버전: `2.2.0`
- Python: 3.10 이상
- 지원 환경: macOS 단일 사용자
- 라이선스: MIT

> Agent Hub는 Anthropic, xAI, Google, OpenAI의 공식 제품이 아닙니다. 모델 사용량, 요금,
> 데이터 처리 조건은 각 provider의 정책을 따릅니다.

## 주요 특징

### 하나의 방식으로 네 provider 사용

Provider마다 로그인과 API 동작은 다르지만, Agent Hub 밖에서는 같은 작업 형식으로 다룹니다.
호스트에 provider별 MCP를 따로 노출하지 않고 14개의 공통 도구만 제공합니다.

### 중간에 끊겨도 이어지는 작업

긴 작업은 run, step, event 단위로 SQLite에 저장됩니다. 호스트의 MCP 연결이 끊겨도
백그라운드의 `agent-hubd`가 실행 상태를 유지하며, 완료된 단계는 다시 실행하지 않습니다.

### 외부 전송 전 사람 확인

저장소 파일이나 이전 결과물을 모델에 보낼 때는 포함할 파일, 데이터 크기, 목적 provider와
내용 digest를 먼저 만듭니다. 연결 GUI에서 사람이 이 목록을 승인한 뒤 같은 digest, policy
revision과 일회용 review ID를 제출해야 외부 모델을 호출합니다. 승인 전에는 provider 호출이
발생하지 않으며, 한 번 사용한 승인은 다시 사용할 수 없습니다.

### Provider별 격리

Claude, Grok, Gemini, GPT는 각각 별도 worker process에서 실행됩니다. 한 provider가
비정상 종료되어도 daemon과 다른 provider에 영향을 주지 않도록 분리했습니다. macOS에서는
외부 TCP/UDP를 허용 도메인 proxy로 제한하고, worker가 홈 디렉터리의 다른 자격 정보 경로를
읽지 못하도록 막습니다. 직접 DNS 조회와 사용자·임시 디렉터리의 Unix socket 연결도
차단합니다.

### 결과의 출처와 판단 근거 보존

결과물은 암호화된 artifact로 저장되며, 어떤 step과 입력에서 만들어졌는지 함께 기록됩니다.
라우팅 결과에는 선택한 provider뿐 아니라 후보, 제외 이유, 점수와 표본 수도 남습니다.
`HANDOFF.md`에는 다음 작업자가 알아야 할 결정과 검증 결과만 정리합니다.

## 빠르게 시작하기

### 1. 설치

```bash
git clone https://github.com/Meapri/agent-hub.git
cd agent-hub
./scripts/bootstrap.sh
```

`bootstrap.sh`는 Python 3.10 이상을 찾아 `.venv`를 만들고 개발 의존성을 설치합니다.
특정 Python을 사용하려면 경로를 지정할 수 있습니다.

```bash
AGENT_HUB_PYTHON=/path/to/python3.12 ./scripts/bootstrap.sh
```

### 2. 프로젝트 설정

Agent Hub의 주요 설정 명령은 바로 파일을 바꾸지 않습니다. 첫 명령으로 변경안과 SHA256
digest를 확인한 뒤, 같은 digest를 `--apply`에 전달해야 실제로 적용됩니다.

```bash
./.venv/bin/agent-hub init --project-root . --json

./.venv/bin/agent-hub init \
  --project-root . \
  --apply \
  --proposal-sha256 검토한_SHA256 \
  --json
```

### 3. Daemon과 MCP 연결

```bash
./.venv/bin/agent-hub setup --repo-root . --json

./.venv/bin/agent-hub setup --repo-root . --apply \
  --proposal-sha256 검토한_SHA256 \
  --json
```

`setup`은 호스트 설정과 macOS LaunchAgent 변경안을 함께 준비합니다. 적용하면 사용자 전용
daemon이 시작되고 Codex, Claude Code 등의 MCP 요청이 Agent Hub로 연결됩니다.

```bash
./.venv/bin/agent-hub doctor --project-root . --live --json
```

### 4. Provider 연결

```bash
./.venv/bin/agent-hub-connect
```

브라우저에 로컬 연결 관리 화면이 열립니다. 여기에서 provider별 로그인, 다시 로그인,
로그아웃, 동의, 모델 선택과 실제 생성 테스트를 관리할 수 있습니다.

## 구조

```mermaid
flowchart LR
    Host["Codex · Claude Code · MCP host"] --> Bridge["agent-hub-mcp"]
    Bridge --> Daemon["agent-hubd"]

    Daemon --> Policy["정책 · 외부 전송 승인"]
    Daemon --> Store["SQLite · Artifact · HANDOFF"]
    Daemon --> Planner["Planner · Validator · Router"]

    Planner --> Workers["격리된 Provider Worker"]
    Workers --> Claude["Claude"]
    Workers --> Grok["Grok"]
    Workers --> Gemini["Gemini"]
    Workers --> GPT["GPT"]
```

| 구성 요소 | 역할 |
| --- | --- |
| `agent-hub-mcp` | 호스트의 stdio 요청을 로컬 Unix socket으로 전달합니다. |
| `agent-hubd` | 계획, 정책, 실행, 복구와 결과물을 관리하는 백그라운드 daemon입니다. |
| Provider worker | 각 provider를 별도 process에서 실행합니다. |
| SQLite store | Run, step, event, routing, feedback과 artifact 정보를 저장합니다. |
| 연결 GUI | 계정 연결과 모델 선택, 실제 생성 여부를 관리합니다. |

기본 상태 디렉터리는 `~/.agent-hub`입니다. Unix socket은
`~/.agent-hub/run/agent-hub.sock`, 실행 DB는 `~/.agent-hub/state.sqlite3`에 있습니다.
상태 디렉터리는 `0700`, socket과 DB는 `0600` 권한으로 제한됩니다.

## Provider와 모델 상태

| Provider | 주요 capability | 인증을 관리하는 곳 |
| --- | --- | --- |
| Claude | chat, search, vision, write, review, decide | Claude Code |
| Grok | chat, search, vision, image, write, review, decide | Agent Hub |
| Gemini | chat, search, vision, image, write, review, decide | Agent Hub |
| GPT | chat, vision, write, review, decide | Codex |

Agent Hub는 “연결됨” 하나만으로 provider 상태를 판단하지 않습니다.

- `auth_state`: 현재 인증으로 실제 호출이 가능한지 나타냅니다.
- `catalog_state`: 모델 목록이 live, cached, static fallback, unavailable 중 어느 상태인지
  나타냅니다.
- `generation_state`: 선택한 모델의 짧은 생성 테스트가 verified, failed, unknown 중 어느
  상태인지 나타냅니다.

모델 목록이 보이는 것과 실제 생성이 성공하는 것은 다른 상태입니다. 중요한 모델은 연결 GUI의
generation test로 확인하는 편이 안전합니다. 내부 placeholder model ID는 생성 요청 전에
거부됩니다.

GUI를 사용할 수 없는 환경에서는 다음 명령으로 동의와 로그인을 진행할 수 있습니다.

```bash
./.venv/bin/claude-codex-consent grant --i-understand-and-consent
./.venv/bin/grok-codex-consent grant --i-understand-and-consent
./.venv/bin/google-antigravity-consent grant --i-understand-and-consent
./.venv/bin/openai-codex-consent grant --i-understand-and-consent

claude auth login --claudeai
codex login
codex login --device-auth

./.venv/bin/python scripts/grok_codex_login.py interactive
./.venv/bin/python scripts/google_antigravity_login.py interactive
```

Plugin을 직접 등록해야 한다면 다음 명령을 사용합니다.

```bash
codex plugin marketplace add /absolute/path/to/agent-hub
codex plugin add agent-hub@agent-hub

claude plugin marketplace add /absolute/path/to/agent-hub
claude plugin install agent-hub@agent-hub --scope user
```

## 작업이 실행되는 방식

한 번에 끝나는 요청은 `agent_hub_execute`로 실행합니다. 여러 단계가 필요한 작업은 다음
흐름을 사용합니다.

1. `agent_hub_plan`의 `prepare`가 선택한 파일을 조사하고 외부 전송 변경안을 만듭니다.
2. 연결 GUI에서 사용자가 파일 목록, 목적 provider, 크기와 digest를 검토해 승인하거나
   거부합니다.
3. 같은 digest, policy revision과 `approval_request_id`로 `apply`를 실행하면 planner가
   작업 단계를 제안합니다.
4. 로컬 validator가 의존 관계, capability, 정책과 사용 한도를 검사합니다.
5. `agent_hub_start`가 durable run을 만들고 `agent_hub_continue`가 준비된 단계를 실행합니다.
6. `agent_hub_get`, `agent_hub_events`, `agent_hub_artifact`로 상태와 결과를 확인합니다.

외부 요청이 실제로 전달됐는지 알 수 없는 timeout이나 worker crash가 발생하면 자동으로 같은
요청을 다시 보내지 않습니다. 이런 경우에는 `outcome_unknown`으로 멈춰 중복 생성 가능성을
사용자가 판단할 수 있게 합니다.
Provider가 명확한 retryable 실패를 반환한 step은 `agent_hub_continue`의
`retry_failed_steps`에 step ID를 명시해 다시 실행할 수 있습니다. 안전 여부가 불명확한
`outcome_unknown`이나 내부 오류는 이 경로로 재시도할 수 없습니다.

## 공개 MCP 도구 14개

| 도구 | 역할 |
| --- | --- |
| `agent_hub_status` | Daemon, DB, provider와 설치 상태를 확인합니다. |
| `agent_hub_catalog` | Provider, model, capability와 검증 상태를 조회합니다. |
| `agent_hub_execute` | 짧은 단일 작업을 실행합니다. |
| `agent_hub_plan` | 로컬 조사와 외부 전송 변경안을 준비하고, digest 재확인 후 계획을 만듭니다. |
| `agent_hub_start` | 승인된 계획으로 durable run을 시작합니다. |
| `agent_hub_continue` | 예상 revision을 확인하고 다음 실행 단계를 진행하거나 안전한 failed step을 명시적으로 재시도합니다. |
| `agent_hub_get` | Run과 step의 현재 상태를 읽습니다. |
| `agent_hub_events` | 민감한 본문을 제외한 event를 cursor 방식으로 조회합니다. |
| `agent_hub_cancel` | Run과 실행 중인 worker를 취소합니다. |
| `agent_hub_artifact` | 결과물 조회, 검증, 내보내기와 보관 정책을 다룹니다. |
| `agent_hub_feedback` | 평점과 채택·검증 결과를 기록합니다. |
| `agent_hub_policy` | 프로젝트 정책을 조회하거나 prepare/apply 방식으로 변경합니다. |
| `agent_hub_handoff` | `HANDOFF.md` 갱신과 takeover를 관리합니다. |
| `agent_hub_doctor` | 상태를 읽기 전용으로 진단하고 수리 계획을 만듭니다. |

상태를 바꾸는 도구는 `idempotency_key` 또는 `expected_revision`을 요구합니다. 오류 응답에는
안전한 코드와 다음 행동만 포함하며, credential, 원본 prompt, 결과 본문과 raw exception은
event에 기록하지 않습니다.

## 라우팅과 프로젝트 정책

기본 설정인 `quality_balanced`는 품질 60%, 신뢰성 20%, 지연시간 10%, token 효율 10%를
기준으로 후보를 비교합니다. `shadow` mode에서는 planner 선택을 바꾸지 않고 대안 점수만
기록합니다. `advisory`는 추천을 보여 주며, `auto`는 충분한 표본과 품질 기준을 만족할 때만
provider 순서를 조정합니다. Mode는 자동으로 승격되지 않습니다.

프로젝트별 설정은 `.agent-hub/project.toml`에 둡니다.

```toml
schema = "agent_hub_project_policy_v2"
revision = 0
routing_profile = "quality_balanced"
routing_mode = "shadow"
provider_allowlist = ["claude", "grok", "gemini", "gpt"]
model_allowlist = []
artifact_retention = "durable_private"

[egress]
repository_content = "approval_required"
artifact_content = "approval_required"
inline_prompt = "allowed"

[budgets]
timeout_seconds = 1790
max_leaf_calls = 100
max_tokens = 131072
```

Provider와 model 허용 목록, 외부 전송 규칙, 최대 실행 시간, provider 호출 횟수, token과
결과물 보관 방식을 프로젝트마다 다르게 설정할 수 있습니다.

## 보안과 데이터 경계

- 저장소 내용을 외부로 보내기 전 `egress_manifest_v2`를 연결 GUI에 표시하고, 사람이 승인한
  일회용 review ID와 digest·policy revision 재확인을 요구합니다.
- Secret으로 보이는 줄은 fact pack과 로컬 색인에 넣기 전에 제외합니다.
- 긴 작업의 입력과 결과 artifact는 AES-GCM으로 암호화합니다.
- 암호화 key는 macOS Keychain에 저장합니다. SQLite DB 전체가 암호화되는 것은 아닙니다.
- 로컬 검색은 SQLite FTS5를 사용하며 기본 설정에서 cloud embedding을 호출하지 않습니다.
- Provider worker는 허용된 외부 도메인만 통과시키는 localhost proxy를 사용합니다.
- Third-party provider는 package와 manifest digest, 권한을 검토하기 전에는 설치하지 않습니다.
- Agent Hub core에는 임의 shell 명령 실행 기능이 없습니다.

Agent Hub의 격리 대상은 provider process, 홈 디렉터리의 민감 경로와 외부 통신입니다.
worker 실행에 필요한 system runtime 파일과 일부 macOS system socket은 사용할 수 있지만,
직접 DNS 조회와 사용자 홈·임시 디렉터리의 Unix socket 연결은 차단합니다. 같은 macOS 사용자
권한을 이미 획득한 악성 process까지 막는 다중 사용자 sandbox는 아닙니다.

## Artifact와 HANDOFF

`artifact_v2`에는 내용 digest, 민감도, 생성 step, 입력 참조, 검증 결과, 보관 기간과
내보내기 이력이 들어갑니다. 파일로 내보낼 때도 대상 경로와 digest를 먼저 확인한 뒤
적용합니다.

`HANDOFF.md`는 실행 DB의 복사본이 아닙니다. Git에 남길 목표, 결정, 완료 증거, 검증 결과,
현재 위험과 다음 한 행동을 기록합니다. Agent Hub는 파일 전체 SHA와 관리 영역 SHA를 함께
검사하므로 다른 사람이 수정한 내용을 조용히 덮어쓰지 않습니다.

## 진단, 업데이트와 복구

Lifecycle 명령은 기본적으로 변경안을 출력하는 dry-run입니다. 실제 변경은 검토한 digest를
`--apply --proposal-sha256`에 전달해야 시작됩니다.

```bash
./.venv/bin/agent-hub doctor --project-root . --json
./.venv/bin/agent-hub repair --json
./.venv/bin/agent-hub index --project-root . --json

./.venv/bin/agent-hub stage-release --repo-root . --json
./.venv/bin/agent-hub stage-release --repo-root . --apply \
  --proposal-sha256 검토한_SHA256 --json

./.venv/bin/agent-hub update \
  --candidate-root /absolute/path/to/candidate \
  --json

./.venv/bin/agent-hub rollback --json
```

`stage-release`는 source와 실행 파일 digest를 고정한 별도 runtime을 만듭니다. `update`는
현재 DB의 임시 복사본으로 후보 daemon을 점검하고, 전환 전 runtime과 DB snapshot을 rollback
slot에 보관합니다. 전환이나 복구가 완전히 끝나지 않으면 성공으로 처리하지 않습니다.

## 현재 지원 범위

- macOS 단일 사용자와 사용자당 daemon 하나를 우선 지원합니다.
- Built-in provider의 생성 요청은 idempotent하다고 가정하지 않습니다.
- Model과 quota 정보는 upstream 상태에 따라 달라질 수 있습니다.
- `static_fallback` catalog는 live 응답이 아닙니다.
- `isolated_tool_worker`, `local_model`, `remote_worker`는 기본적으로 꺼진 실험 기능입니다.

## 개발과 검증

```bash
./.venv/bin/ruff check src tests
./.venv/bin/ruff format --check src tests
./.venv/bin/python -m pytest
./scripts/check-sync.sh
./scripts/check-hub-plugins.sh
./.venv/bin/python -m build
```

사용자 문서는 다음 검사도 함께 실행합니다.

```bash
./.venv/bin/python -m orchestrate_codex.document_quality README.md
```

주요 코드는 다음 경로에서 찾을 수 있습니다.

- MCP 도구: `src/agent_hub/v2/tools.py`
- Daemon과 실행 엔진: `src/agent_hub/v2/daemon.py`, `src/agent_hub/v2/service.py`
- 실행 저장소: `src/agent_hub/v2/store.py`
- Provider runtime: `src/agent_hub/v2/provider_worker.py`,
  `src/agent_hub/v2/provider_runtime.py`
- 정책과 외부 전송: `src/agent_hub/v2/policy.py`, `src/agent_hub/v2/egress.py`
- 연결 GUI: `src/agent_hub/connect_app.py`, `src/agent_hub/connect_service.py`
- 프로토콜: `docs/architecture/agent-hub-v2-protocol.md`

## 라이선스

MIT License입니다. 자세한 내용은 `LICENSE`와 `NOTICE.md`를 확인하세요.
