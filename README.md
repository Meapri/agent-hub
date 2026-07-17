# Agent Hub

Agent Hub는 Claude, Grok, Gemini를 하나의 MCP 서버(`agent-hub-mcp`)로 묶어 쓰는 개인용 멀티모델 작업 환경입니다. Anthropic, xAI, Google이 만든 공식 제품은 아닙니다.

## Agent Hub는 무엇을 하나요

MCP 클라이언트에는 `agent_hub_*` 이름이 붙은 도구 26개만 나타납니다. 모델마다 안에서 쓰는 도구는 밖으로 내보내지 않습니다. 세 모델 서비스를 같은 창구로 불러 대화, 검색, 글쓰기, 이미지 작업, 모델 비교, Git 변경 검토, 릴리스 문서 작성을 한자리에서 처리합니다.

아래 표의 지원 표시는 Agent Hub 연결 코드에 들어 있는 기능을 뜻합니다. 실제로 쓸 수 있는지는 계정 권한과 구독 한도에 따라 달라집니다.

| 기능 | Claude | Grok | Gemini |
|------|:------:|:----:|:------:|
| 대화 | O | O | O |
| 이미지 분석 | O | O | O |
| 검색 | O | O | O |
| 글쓰기 | O | O | O |
| 모델 비교 | O | O | O |
| Git 변경 검토 | O | O | O |
| 릴리스 문서 작성 | O | O | O |
| 이미지 생성 | - | O | O |

릴리스 스냅샷은 로컬에서 만듭니다. 모델별로 바꿀 수 있는 설정은 다음과 같습니다.

- Claude: `model`, `temperature`, `max_tokens`
- Grok: `model`, `temperature`, `max_tokens`, `api_mode`
- Gemini: `model`, `transport`, `profile`, `temperature`, `max_tokens`

자동 계획 모드는 저장된 기본값과 별도로 각 단계의 추론 강도를 `low`, `medium`, `high` 중에서 고릅니다. 같은 값이라도 모델 서비스에 전달되는 형식은 다릅니다.

| 모델 서비스 | 실제 요청 값 |
|-------------|--------------|
| Claude | `output_config.effort`, Opus 4.8은 adaptive thinking 함께 사용 |
| Grok | Responses API의 `reasoning.effort` |
| Gemini | `thinking_level` |

설정한 모델이 이 기능을 받지 못하면 값을 조용히 버리지 않고 요청을 실패로 처리합니다.

Claude와 Grok의 자체 검색, Grok의 이미지 생성은 계정에 별도 API 권한이 있어야 동작할 수 있습니다. `provider=auto`로 두고 모델을 적지 않으면 Claude를 씁니다.

## 시작하기 전에 준비할 것

- Python 3.10 이상을 설치하세요.
- Node.js와 `npx`가 필요합니다. Ruler 규칙을 동기화할 때 씁니다.
- `uv` 또는 `uvx`는 로컬 공유 메모리를 쓸 때만 필요합니다.
- 쓰려는 모델 서비스의 계정, 구독 또는 API 키를 준비하세요.

## 저장소 설치

저장소를 clone하고 가상환경에서 의존성을 설치한 뒤 테스트를 돌립니다.

```bash
git clone https://github.com/Meapri/agent-hub.git
cd agent-hub
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
./.venv/bin/pytest -q
```

설치가 끝나면 `.venv/bin/agent-hub-mcp` 실행 파일이 생깁니다.

`instructions/.ruler/ruler.toml`에서 경로 세 곳을 clone한 위치에 맞게 바꾸세요. `BASIC_MEMORY_CONFIG_DIR`, `BASIC_MEMORY_HOME`, `mcp_servers.agent-hub.command`입니다. 그다음 규칙을 배포하고 결과를 확인합니다.

```bash
./scripts/sync.sh
./scripts/check-sync.sh
```

## 모델 서비스 사용 동의

모델 서비스마다 사용 동의를 따로 받아야 합니다. 쓰려는 서비스에 대해 아래 명령을 실행하세요.

```bash
./.venv/bin/claude-codex-consent grant --i-understand-and-consent
./.venv/bin/grok-codex-consent grant --i-understand-and-consent
./.venv/bin/google-antigravity-consent grant --i-understand-and-consent
```

동의를 되돌릴 때는 `grant` 자리에 `revoke`를 넣습니다.

## 로그인

모델 서비스마다 로그인 방식이 다릅니다. 쓰는 서비스만 로그인하면 됩니다.

Claude:

```bash
claude auth login --claudeai
./.venv/bin/python scripts/claude_codex_login.py mirror-keychain
./.venv/bin/python scripts/claude_codex_login.py status
```

Grok:

```bash
./.venv/bin/python scripts/grok_codex_login.py interactive
./.venv/bin/python scripts/grok_codex_login.py status
```

Google Antigravity:

```bash
./.venv/bin/python scripts/google_antigravity_login.py interactive
./.venv/bin/python scripts/google_antigravity_login.py status
```

설치 환경을 한 번에 점검하려면 doctor를 실행하세요.

```bash
./scripts/doctor.sh
```

doctor는 규칙 동기화, Python 패키지, basic-memory, MCP 실행 파일, 메모리 저장소를 확인합니다. 모델별 동의와 로그인 상태는 MCP를 연결한 뒤 `agent_hub_status`로 봅니다.

`agent_hub_status`에서 실시간 확인을 요청하면 Gemini의 저장된 액세스 토큰이 만료됐을 때 갱신 토큰으로 먼저 갱신한 뒤, 같은 응답에서 새 인증 상태를 보여 줍니다. 갱신 토큰까지 없거나 갱신이 실패하면 준비 완료로 표시하지 않습니다.

## 앱에 플러그인 연결하기

MCP 클라이언트에 Agent Hub를 플러그인으로 등록합니다. `<REPO_ROOT>`는 clone한 저장소 경로로 바꿔 주세요.

Codex:

```bash
codex plugin marketplace add <REPO_ROOT>
codex plugin add agent-hub@agent-hub
codex plugin list
```

Claude Code:

```bash
claude plugin marketplace add <REPO_ROOT>
claude plugin install agent-hub@agent-hub --scope user
claude plugin list
```

설정 예시는 `hubs/codex/`와 `hubs/claude-code/`에 있습니다.

## 첫 요청 보내기

연결이 끝났으면 세 도구를 순서대로 써서 첫 요청까지 확인합니다.

- `agent_hub_status`로 쓰려는 모델 서비스의 동의·로그인·ready 상태를 봅니다.
- `agent_hub_list_workflows`로 작업 흐름 5종과 실행 옵션을 확인합니다.
- `agent_hub_chat`으로 로그인한 모델 서비스에 첫 요청을 보냅니다.

쓰지 않는 모델 서비스는 not ready 상태로 둬도 됩니다. 대화에는 영향을 주지 않습니다. basic-memory도 대화 기능에 꼭 필요하지는 않습니다.

## 세 가지 실행 방식

작업 성격에 따라 실행 방식을 고릅니다.

- 직접 호출: 도구 하나로 끝나는 작업에 씁니다.
- 고정 작업 흐름: 순서가 정해진 반복 작업에 씁니다.
- 자동 계획 모드(`adaptive`): 일을 어떻게 나눌지부터 모델이 판단하는 작업에 씁니다.

자동 계획 모드에서는 계획 모델이 단계, 담당 모델 서비스, 단계 사이의 앞뒤 관계, 실패했을 때 쓸 대체 모델을 정합니다. 계획이 나오면 로컬 검사가 다음을 확인합니다.

- 허용된 기능과 모델만 썼는지
- 서로 계속 기다리는 단계나 결과에 연결되지 않은 단계가 없는지
- 마지막 결과를 만드는 단계가 하나인지
- 설정한 최대 호출 횟수를 넘지 않는지
- 단계별 추론 강도와 코드 조사 깊이가 허용된 값인지

필요한 앞 단계가 모두 끝난 작업만 동시에 실행합니다. 한 모델이 실패하면 계획에 적힌 대체 모델을 시도하고, 그마저 모두 실패하면 그 결과가 필요한 뒤 작업은 시작하지 않습니다. 검사를 통과한 plan에는 `plan_sha256`이 붙습니다. 같은 plan을 실행에 넘기면 계획 모델을 다시 부르지 않습니다.

### 코드베이스를 읽고 문서 쓰기

프로젝트 전체를 설명하는 README나 기술 문서는 코드 조사부터 맡기는 편이 안전합니다. 자동 계획 모드는 로컬 저장소 조사에 `inspect_codebase`를 사용합니다. `agent_hub_search`는 웹 자료를 찾는 기능이라 로컬 코드 조사 대신 쓰지 않습니다.

`inspect_codebase`에는 세 가지 조사 깊이가 있습니다.

| 값 | 살펴보는 범위 |
|----|---------------|
| `shallow` | 작은 수정이나 한 기능을 확인할 때 필요한 핵심 파일 |
| `standard` | 주요 진입점, 설정, 구현, 대표 테스트 |
| `deep` | 저장소 전체 구조를 훑은 뒤 관련성이 높은 핵심 파일의 전문 또는 관련 함수 구간까지 재확인 |

planner는 작업 범위와 불확실성을 보고 조사 깊이와 `reasoning_effort`를 단계마다 정합니다. 조사 요청에 나온 파일명, 함수명, 기능을 바탕으로 관련 파일을 다시 고르고, 작은 핵심 파일은 전문을 읽습니다. 큰 파일은 관련 함수 주변을 여러 구간으로 나눠 읽습니다. 모든 코드 조각에는 원본 줄 번호와 `complete` 또는 `partial` 표시가 붙으므로, 모델이 보지 않은 뒷부분을 본 것처럼 쓰지 못합니다.

조사 결과에는 전체 후보 수, 실제로 읽은 파일, 전문을 읽은 파일, 일부만 읽은 파일과 줄 범위가 남습니다. 저장소 전체 파일 수와 문맥 길이에는 단계별 상한이 있으며, `deep`도 무제한으로 파일을 전송하지 않습니다.

문서 작업을 계획할 때는 아래 값을 함께 넘기면 됩니다.

```json
{
  "workflow_id": "adaptive",
  "prompt": "코드 근거를 빠짐없이 확인한 뒤 자연스러운 한국어 README를 작성해 줘",
  "project_root": "/absolute/path/to/repository",
  "policy_mode": "required",
  "max_steps": 6,
  "models": {
    "claude": "claude-opus-4-8",
    "grok": "grok-4.5",
    "gemini": "gemini-3.1-pro-high"
  }
}
```

`agent_hub_plan_workflow`로 plan을 확인한 다음 그대로 `agent_hub_run_workflow`에 넘깁니다. 실행할 때 `max_leaf_calls`는 plan의 `expected_max_calls` 이상으로 잡아야 합니다. 더 작게 주면 모델을 부르기 전에 로컬 검사가 실행을 거부합니다.

아래 값으로 한 작업에서 사용할 모델 호출 범위를 제한합니다.

| 파라미터 | 뜻 |
|----------|-----|
| `max_concurrency` | 한 번에 실행하는 단계 수 |
| `max_leaf_calls` | 한 작업에서 모델을 부르는 최대 횟수 |
| `per_call_timeout` | 한 번의 호출을 기다리는 시간 |
| `max_tokens` | 응답 길이 제한 |

`reasoning_effort`의 뜻은 다음과 같습니다.

- `low`: 형식 변환이나 짧은 확인처럼 판단이 적은 단계
- `medium`: 일반적인 분석과 작성
- `high`: 구조가 복잡한 코드 조사, 어려운 검토, 여러 조사 결과의 최종 정리

## 합의 검사(Consistency Gate)

approve/reject처럼 선택지가 미리 정해진 판단에만 씁니다. 자유롭게 쓴 글에 품질 점수를 매기는 기능이 아닙니다.

합의 검사는 필요한 응답 수, 같은 프로젝트 규칙과 같은 요청을 받았는지, 정해 둔 JSON 응답 형식과 합의율을 검사합니다. 한 응답이라도 형식을 어기거나 기준에 못 미치면 결론을 만들지 않고 사람 확인이 필요하다고 돌려줍니다.

합의 검사를 쓰지 않은 일반 비교에서 일부 모델만 실패하면, 성공한 결과는 남기고 `partial_compare_failures` warning을 붙이기도 합니다.

## 작업 흐름 5종

| 작업 흐름 | 실행 옵션 | 설명 |
|-----------|-----------|------|
| `repo_document` | readme, technical-doc, proposal | 저장소 문서 작성 |
| `git_document` | pr-description, release-notes | Git 변경 기반 문서 작성 |
| `research_brief` | default | 조사 브리프 작성 |
| `deep_readme` | default | Claude 구조 분석 → Grok 사용성 분석 → Gemini 작성 → 검증 |
| `adaptive` | llm-planned | 자동 계획 모드 실행 |

readme, pr-description 같은 이름은 실행 옵션이며, 작업 흐름 수에 더하지 않습니다.

작업 흐름은 두 방식으로 돌립니다.

- 단계별 실행: `agent_hub_plan_workflow` → `agent_hub_start_workflow` → `agent_hub_continue_workflow` → `agent_hub_verify`.
- 자동 실행: `agent_hub_run_workflow`.

자동 실행도 모델 서비스별 동의와 인증을 확인합니다.

## 공개 도구 26개

인증·상태 6개:

- `agent_hub_status`, `agent_hub_list_models`, `agent_hub_auth_start`, `agent_hub_auth_complete`, `agent_hub_auth_refresh`, `agent_hub_auth_logout`

직접 작업 8개:

- `agent_hub_chat`, `agent_hub_search`, `agent_hub_write`, `agent_hub_generate_image`, `agent_hub_compare_models`, `agent_hub_review_diff`, `agent_hub_release_snapshot`, `agent_hub_release_draft`

설정 3개:

- `agent_hub_get_settings`, `agent_hub_update_settings`, `agent_hub_reset_settings`

작업 흐름 9개:

- `agent_hub_list_workflows`, `agent_hub_get_workflow`, `agent_hub_plan_workflow`, `agent_hub_start_workflow`, `agent_hub_continue_workflow`, `agent_hub_get_run`, `agent_hub_run_workflow`, `agent_hub_delegate`, `agent_hub_verify`

## 도구 응답 구조

모든 도구는 MCP `content[]`, `isError`, `structuredContent`를 돌려줍니다. `structuredContent`의 공통 필드는 다음과 같습니다.

- `success`, `operation`, `provider`, `model`, `text`, `finish_reason`, `usage`, `warnings`, `error`, `artifacts`, `data`

출력 한도에 걸려 결과가 잘리면 `success=false`와 함께 `incomplete_finish_reason` warning이 붙습니다. 잘린 결과는 완료로 처리하지 마세요.

## 이미지 입력 규칙

이미지를 넣을 때는 `images`에 파일 경로를 주고, `workspace_root`에 그 파일이 든 작업 폴더의 절대경로를 줍니다. 지원 형식은 JPEG, PNG, GIF, WebP이고, 파일 하나는 20 MiB 이하여야 합니다. 파일은 `workspace_root` 안에 있어야 합니다.

## 규칙과 기록이 저장되는 위치

- `instructions/.ruler/`: 여러 클라이언트에 배포하는 공통 AI 규칙.
- `AGENTS.md`, `CLAUDE.md`: 배포로 생성된 클라이언트 규칙.
- `HANDOFF.md`: 현재 진행 상태와 다음 작업.
- `memory/data/`: 결정과 반복해서 피해야 할 실수.
- Git: 코드와 변경 이력.

basic-memory는 보조 검색 기능입니다. 기본 설정은 임베딩 검색을 끄고 FTS 텍스트 검색만 씁니다.

## 보안

- 모델 서비스마다 동의를 따로 확인합니다. 동의하지 않은 서비스를 부르면 실행을 거부합니다.
- OAuth 토큰과 API 키는 사용자 설정 디렉터리, Keychain, 환경변수에만 둡니다. 저장소에 커밋하지 마세요.
- 파일과 Git 변경을 읽는 도구에는 workspace 절대경로를 줍니다.
- 홈 디렉터리 전체, 파일시스템 루트, 인증 경로는 workspace root로 쓰지 마세요.
- 여러 모델을 부르는 작업은 호출 횟수, 동시 실행 수, 대기 시간, 응답 길이를 제한합니다.
- 테스트가 통과해도 구독 한도나 모델 응답 품질을 보장하지는 않습니다.

## orchestrate_* 등 이전 이름을 쓰고 있다면

`orchestrate_*`, `claude_codex_*`, `grok_codex_*`, `google_antigravity_*`를 아직 부르고 있다면 `agent_hub_*`로 바꾸세요. 통합 전 이름은 `agent-hub-mcp`에 등록되지 않아, 그대로 부르면 `unknown tool`이 돌아옵니다.

## 저장소 구조

- `src/agent_hub/`: 통합 MCP 서버와 공개 도구.
- `src/orchestrate_codex/`: 작업 흐름, 실행 상태, 검증.
- `src/claude_codex/`, `src/grok_codex/`, `src/google_antigravity_codex/`: 모델별 연결 코드.
- `tests/`: 단위·통합·MCP 형식 테스트.
- `instructions/.ruler/`, `memory/data/`, `handoff/`, `hubs/`, `scripts/`, `plugins/`, `model-access/`, `HANDOFF.md`.

## 개발용 검증 명령

```bash
./.venv/bin/ruff check src tests
./.venv/bin/pytest -q
./scripts/check-sync.sh
./scripts/test-phase1.sh
./scripts/doctor.sh
./.venv/bin/python -m orchestrate_codex.document_quality README.md
./.venv/bin/python -m build
```

## 라이선스

MIT 라이선스로 배포합니다. 통합 구성요소의 출처와 저작권은 `NOTICE.md`에 있습니다.
