# Agent Hub

Agent Hub는 Claude, Grok, Gemini를 하나의 MCP 서버로 연결하는 개인용 멀티모델 작업 환경입니다. Claude Code와 Codex 어느 쪽에서 작업하더라도 같은 도구, 같은 프로젝트 규칙, 같은 인계 기록을 사용할 수 있게 구성되어 있습니다.

MCP(Model Context Protocol)는 AI 코딩 앱이 외부 도구를 호출하는 표준 연결 방식입니다. Agent Hub를 설치하면 앱에는 provider별 내부 구현 대신 `agent_hub_*` 공용 도구 26개만 보입니다. 인증, 모델 설정, 병렬 실행, 실패 처리처럼 provider마다 다른 부분은 서버 안에서 처리합니다.

이 프로젝트는 Anthropic, xAI, Google의 공식 제품이 아닙니다.

## 해결하는 문제

여러 모델과 코딩 앱을 따로 연결하면 도구 이름과 인증 방식이 달라지고, 앱을 바꿀 때 작업 규칙과 진행 상태가 끊기기 쉽습니다. Agent Hub는 이 문제를 세 층으로 나눠 해결합니다.

- **통합 모델 도구**: 대화, 검색, 문서 작성, 이미지 생성, 모델 비교, Git 검토를 `agent_hub_*` 인터페이스로 제공합니다.
- **검증된 다단계 실행**: 계획 모델이 작업 단계를 만들고, 로컬 검증기가 의존 관계와 호출 횟수 제한을 확인한 뒤 실행합니다.
- **파일 기반 연속성**: 프로젝트 규칙은 `instructions/.ruler/`, 진행 상태는 `HANDOFF.md`, 장기 기억은 `memory/data/`에 둡니다.

모델이나 앱은 교체할 수 있지만, 작업의 정본은 Git에 남는다는 원칙을 따릅니다.

## 지원 기능

| 기능 | Claude | Grok | Gemini |
|---|:---:|:---:|:---:|
| 대화와 이미지 입력 | 지원 | 지원 | 지원 |
| 웹 검색 | 지원¹ | 지원¹ | 지원 |
| 문서 작성과 Git 변경 검토 | 지원 | 지원 | 지원 |
| 모델 비교와 릴리스 문서 작성 | 지원 | 지원 | 지원 |
| 이미지 생성 | 미지원 | 지원¹ | 지원 |

¹ 해당 서비스 계정의 API 권한이 필요할 수 있습니다.

대화 작업에는 `reasoning_effort=low|medium|high`를 지정할 수 있습니다. Agent Hub는 이 값을 Claude의 `output_config.effort`, Grok Responses API의 `reasoning.effort`, Gemini의 `thinking_level`로 변환합니다. 선택한 모델이 요청한 설정을 지원하지 않으면 값을 버리지 않고 오류를 반환합니다.

## 설치

### 준비물

- Python 3.10 이상
- 프로젝트 규칙 동기화에 사용할 Node.js와 `npx`
- 공유 메모리를 사용할 경우 `uvx`
- 사용할 provider의 계정과 구독 또는 API 권한

저장소를 클론하고 가상환경에 개발 의존성을 설치합니다.

```bash
git clone https://github.com/Meapri/agent-hub.git
cd agent-hub
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
uv tool install basic-memory
```

`pip install -e .`만 실행해도 Agent Hub 서버와 동의 명령은 설치됩니다. 테스트, Ruff, 빌드 도구까지 필요하면 위 예시처럼 `.[dev]`를 사용하세요.

### 로컬 경로 맞추기

MCP 설정에는 이 컴퓨터의 절대경로가 들어갑니다. 저장소를 다른 위치에 클론했다면 다음 파일의 `agent-hub-mcp`, `BASIC_MEMORY_CONFIG_DIR`, `BASIC_MEMORY_HOME` 경로를 실제 위치에 맞게 바꾸세요.

- `instructions/.ruler/ruler.toml`
- `hubs/codex/.mcp.json`
- `hubs/claude-code/.mcp.json`

Ruler 정본을 각 코딩 앱 설정으로 동기화합니다.

```bash
./scripts/sync.sh
./scripts/check-sync.sh
```

## Provider 동의와 로그인

Provider는 서로 독립적입니다. 사용할 모델만 동의하고 로그인해도 됩니다.

### 1. 로컬 사용 동의

```bash
./.venv/bin/claude-codex-consent grant --i-understand-and-consent
./.venv/bin/grok-codex-consent grant --i-understand-and-consent
./.venv/bin/google-antigravity-consent grant --i-understand-and-consent
```

### 2. 서비스 로그인

Claude는 Claude Code 구독 OAuth를 로컬 자격 증명 파일로 연결합니다.

```bash
claude auth login --claudeai
./.venv/bin/python scripts/claude_codex_login.py mirror-keychain
./.venv/bin/python scripts/claude_codex_login.py status
```

Grok은 브라우저 기반 device-code OAuth를 사용합니다.

```bash
./.venv/bin/python scripts/grok_codex_login.py interactive
./.venv/bin/python scripts/grok_codex_login.py status
```

Google Antigravity는 PKCE OAuth를 사용합니다.

```bash
./.venv/bin/python scripts/google_antigravity_login.py interactive
./.venv/bin/python scripts/google_antigravity_login.py status
```

설치와 로컬 준비 상태는 다음 명령으로 확인할 수 있습니다.

```bash
./scripts/doctor.sh
```

플러그인이 연결된 뒤에는 `agent_hub_status`로 동의, 인증, 기본 모델과 준비 상태를 확인하세요. `probe=true`를 주면 실제 provider 연결도 점검합니다.

## Codex와 Claude Code에 연결

아래의 `/absolute/path/to/agent-hub`를 실제 클론 경로로 바꾸세요.

### Codex

```bash
codex plugin marketplace add /absolute/path/to/agent-hub
codex plugin add agent-hub@agent-hub
codex plugin list
```

### Claude Code

```bash
claude plugin marketplace add /absolute/path/to/agent-hub
claude plugin install agent-hub@agent-hub --scope user
claude plugin list
```

두 플러그인은 다음 MCP 서버 두 개를 등록합니다.

- `agent-hub`: Claude, Grok, Gemini와 다단계 workflow를 제공하는 통합 서버
- `memory`: Git으로 관리하는 로컬 `basic-memory` 서버

연결 후 도구 목록에는 `agent_hub_*` 26개가 보여야 합니다. `claude_codex_*`, `grok_codex_*`, `google_antigravity_*`, `orchestrate_*`는 내부 구현이므로 공용 목록에 노출되지 않습니다.

## 공용 도구

| 영역 | 도구 | 용도 |
|---|---|---|
| 상태 | `agent_hub_status`, `agent_hub_list_models` | 인증 상태, 준비 여부, 사용 가능한 모델 확인 |
| 인증 | `agent_hub_auth_start`, `agent_hub_auth_complete`, `agent_hub_auth_refresh`, `agent_hub_auth_logout` | provider OAuth 시작·완료·갱신·로컬 로그아웃 |
| 생성 | `agent_hub_chat`, `agent_hub_search`, `agent_hub_write`, `agent_hub_generate_image` | 대화, 근거 있는 검색, 문서 작성, 이미지 생성 |
| 검토 | `agent_hub_compare_models`, `agent_hub_review_diff`, `agent_hub_release_snapshot`, `agent_hub_release_draft` | 모델 비교, Git diff 검토, 릴리스 사실 수집과 문서 작성 |
| 설정 | `agent_hub_get_settings`, `agent_hub_update_settings`, `agent_hub_reset_settings` | provider별 모델, 출력 길이, transport와 profile 설정 |
| Workflow | `agent_hub_list_workflows`, `agent_hub_get_workflow`, `agent_hub_plan_workflow`, `agent_hub_start_workflow`, `agent_hub_continue_workflow`, `agent_hub_get_run`, `agent_hub_run_workflow` | 작업 목록, 계획, 일괄 실행과 재개 |
| 위임·검증 | `agent_hub_delegate`, `agent_hub_verify` | 단일 provider 호출 준비, 최종 결과의 로컬 검증 |

`agent_hub_release_snapshot`과 `agent_hub_verify`처럼 모델을 부르지 않는 로컬 도구도 있습니다. 외부 호출이 필요한 도구와 분리해 사용량과 실패 지점을 확인할 수 있습니다.

## 작업 실행 방식

### 단발 작업

질문 하나, 문단 수정, 이미지 생성처럼 한 번의 모델 호출이면 충분한 작업은 `agent_hub_chat`, `agent_hub_write`, `agent_hub_generate_image`를 직접 사용하세요. `provider=auto`를 주면 모델 이름과 기능 지원 여부를 바탕으로 adapter를 선택합니다.

README나 장기 보존 기술 문서를 작성할 때는 `project_root`, `policy_mode=required`, `task=readme|technical-doc`를 반드시 함께 넘기는 편이 안전합니다.

### 고정 Workflow

반복되는 작업에는 미리 정의된 workflow를 사용할 수 있습니다.

| Workflow | 용도 | Preset |
|---|---|---|
| `repo_document` | 저장소 사실 수집 후 문서 작성과 검증 | `readme`, `technical-doc`, `proposal` |
| `git_document` | Git 변경을 바탕으로 문서 작성 | `pr-description`, `release-notes` |
| `research_brief` | 웹 검색 후 출처 기반 요약 작성 | `default` |
| `deep_readme` | 여러 모델이 구조와 사용법을 나눠 조사한 뒤 README 작성 | `default` |

### Adaptive Workflow

범위가 넓거나 작업 순서를 미리 정하기 어려우면 `workflow_id="adaptive"`를 사용하세요. 계획 모델이 provider, 추론 강도, 조사 깊이와 의존 관계를 담은 DAG(방향성 비순환 그래프)를 만듭니다. 로컬 검증기는 다음 조건을 확인한 뒤에만 실행을 허용합니다.

- 허용된 capability와 provider인지
- 존재하지 않는 단계에 의존하지 않는지
- 순환 의존성이 없는지
- 모든 중간 단계가 단 하나의 최종 결과로 이어지는지
- 예상 호출 횟수가 `max_leaf_calls` 안에 들어오는지

먼저 계획만 만듭니다.

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

`agent_hub_plan_workflow`가 반환한 plan에서 조사 단계가 작성 단계의 의존성인지 확인하세요. 짧은 plan은 검토한 plan을 `agent_hub_run_workflow`에 그대로 넘기면 됩니다.

여러 dependency wave가 필요한 긴 plan은 `agent_hub_start_workflow`에 넘기세요. 서버가 plan과 실행 상태를 파일에 저장하고 `run_id`를 반환합니다. 이후에는 다음 호출을 반복합니다.

```json
{
  "run_id": "<start가 반환한 run_id>",
  "max_waves_per_call": 1
}
```

`agent_hub_continue_workflow`는 기본적으로 의존성이 풀린 wave 하나를 병렬 실행하고 다시 저장합니다. 마지막 단계가 끝나면 `status=completed`와 최종 텍스트를 반환합니다. 상태는 기본적으로 `~/.orchestrate_codex/runs/`에 저장되며, `ORCHESTRATE_CODEX_STATE_DIR`로 위치를 바꿀 수 있습니다.

## Timeout과 재개

기본 MCP 호출 제한을 300초로 가정하고, Agent Hub는 반환 여유 10초를 남깁니다. `workflow_timeout` 기본값은 270초이며 기본 환경의 최대값은 290초입니다.

| 환경변수 | 기본값 | 역할 |
|---|---:|---|
| `AGENT_HUB_MCP_CALL_TIMEOUT` | `300` | MCP 클라이언트의 실제 호출 제한 |
| `AGENT_HUB_TIMEOUT_RETURN_MARGIN` | `10` | 상태를 저장하고 응답할 시간 |
| `AGENT_HUB_WORKFLOW_TIMEOUT` | `270` | 한 번의 일괄 실행 또는 재개 slice가 쓸 기본 예산 |

클라이언트 제한이 다르면 서버 환경변수를 실제 값에 맞추세요. 허용되는 `workflow_timeout` 최대값은 MCP 제한에서 반환 여유를 뺀 값으로 계산되어 도구 schema에도 반영됩니다.

재개 가능한 실행은 시간 예산이 끝나도 성공한 단계의 결과를 버리지 않습니다.

- `start`/`continue` 실행은 상태를 `paused`로 저장하고 같은 `run_id`로 다음 wave를 이어갑니다.
- `run_workflow` 일괄 실행은 top-level에서 `timed_out` 실패를 반환하지만, 저장에 성공하면 `resumable=true`, `run_id`, `next_action`도 함께 제공합니다.
- 상태 파일을 저장하지 못하면 재개 가능한 척하지 않고 호출을 실패시킵니다.

중간 단계의 문서 텍스트는 최종 결과로 저장하지 마세요. `status=completed`와 최종 품질 검사를 모두 확인한 뒤 파일을 교체해야 합니다.

## 코드 조사와 문서 품질

Adaptive plan의 `inspect_codebase`는 `shallow`, `standard`, `deep` 조사 깊이를 지원합니다. `deep`은 저장소를 넓게 훑은 뒤 관련성이 높은 파일을 다시 고릅니다. 작은 핵심 파일은 전문을 읽고, 큰 파일은 관련 함수 주변을 줄 번호와 함께 나눠 읽습니다. 일부만 읽은 파일에는 `partial` 표시가 남으므로 보이지 않은 코드를 근거로 쓸 수 없습니다.

`agent_hub_write`는 README와 기술 문서를 durable 문서로 분류해 로컬 품질 검사를 적용합니다. 다음 문제는 차단 오류입니다.

- 비어 있거나 생성 도중 잘린 문서
- 번역투, 작업 과정을 중계하는 문장, 설명 없는 내부 용어
- 최종 문서에 남은 TODO, TBD, FIXME 같은 미완성 표식
- 완전한 저장소 manifest에 존재하지 않는 소스 파일 주장

기본적으로 한 번 전체 재작성을 시도하며, `quality_rewrite_attempts`는 0부터 2까지 조정할 수 있습니다. 최종 결과에는 `quality_gate.passed`, 검사기 버전, 재작성 횟수와 경고가 들어갑니다.

`source_file`의 상대경로는 MCP 서버가 시작된 디렉터리가 아니라 함께 전달한 `workspace_root` 또는 `project_root` 아래에서 해석됩니다.

## Consistency Gate

`agent_hub_compare_models`의 Consistency Gate는 승인/거절처럼 선택지가 정해진 판단에 사용합니다. 자유 형식 글에 임의의 점수를 매기는 기능은 아닙니다.

각 provider는 허용된 label 중 하나를 `decision_v1` JSON으로 반환해야 합니다. 기본 설정은 합의율 100%, 모든 응답 유효, 최소 유효 응답 2개입니다. provider 실패, 잘못된 JSON, 응답 부족, 동률이나 합의 부족이 있으면 사람 검토가 필요하다고 표시합니다.

프로젝트 정책을 주입하면 `policy_sha256`과 `request_sha256`이 결과에 남습니다. 정책 파일은 행동 규칙의 정본이지만 제품 기능의 증거는 아닙니다. 정책과 현재 코드가 충돌하면 저장소 코드와 결정적으로 수집한 fact pack이 우선합니다.

## 작업 문맥 유지

각 저장 영역은 역할이 다릅니다.

- `instructions/.ruler/`: 사람이 검토한 프로젝트 규칙의 정본입니다. `AGENTS.md`, `CLAUDE.md`, `.gemini/settings.json`은 여기서 생성합니다.
- `HANDOFF.md`: 현재 목표, 완료·미완료 상태, 검증 결과, 위험과 다음 한 걸음을 기록합니다.
- `memory/data/`: 장기적으로 다시 찾을 결정, 선호와 교훈을 Markdown으로 저장합니다.

공유 메모리는 `basic-memory`의 로컬 FTS 검색을 사용합니다. `BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED=false`로 시맨틱 검색을 꺼 두어 노트 내용이 외부 임베딩 서비스로 전송되지 않게 구성되어 있습니다. 메모리는 보조 검색 계층이며 규칙이나 현재 진행 상태의 정본으로 사용하지 않습니다.

## 보안 경계

- 각 provider는 명시적 로컬 동의와 인증을 별도로 확인합니다.
- OAuth 토큰과 로컬 설정은 provider별 config 디렉터리에 저장하며 저장소에 커밋하지 않습니다.
- 명시적으로 받은 `project_root`나 `workspace_root`는 파일시스템 루트, 홈 디렉터리 전체, 민감 경로가 될 수 없습니다.
- `.env`, SSH 키, 클라우드 자격 증명, 인증서 개인키 같은 민감 파일은 일반 문서·검토 입력에서 차단합니다.
- 저장소 manifest는 Git 추적 파일과 ignore되지 않은 새 파일만 사용합니다. ignore된 로컬 산출물은 provider 문맥으로 보내지 않습니다.
- 모델별 검색과 이미지 생성은 계정 권한과 사용량을 소비할 수 있습니다. 반복 workflow에는 `max_leaf_calls`를 설정하세요.

## 저장소 구성

```text
agent-hub/
├── src/agent_hub/                 # 공용 MCP API, adaptive 실행, Consistency Gate
├── src/claude_codex/              # Claude adapter
├── src/grok_codex/                # Grok adapter
├── src/google_antigravity_codex/  # Gemini adapter
├── src/orchestrate_codex/         # 고정 workflow와 파일 기반 run store
├── hubs/codex/                    # Codex 플러그인
├── hubs/claude-code/              # Claude Code 플러그인
├── instructions/.ruler/           # 프로젝트 규칙 정본
├── memory/                        # 공유 로컬 메모리
├── scripts/                       # 동기화, doctor, 수용 검사
└── tests/                         # 단위·계약·통합 테스트
```

## 개발과 검증

코드나 문서를 바꾼 뒤에는 관련 검사와 전체 회귀 검사를 실행하세요.

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

`README.md`, `RUN-REPORT.md`, `HANDOFF.md`는 구현과 함께 유지하는 출하물입니다. 테스트가 통과해도 이 세 문서가 현재 동작과 다르면 작업이 끝난 것으로 보지 않습니다.
