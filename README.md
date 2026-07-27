# Agent Hub

Agent Hub는 Claude, Grok, Gemini, GPT를 한곳에서 연결하고 작업에 맞게 조합하는
macOS용 멀티 모델 실행 환경입니다.

여러 모델을 호출하는 데서 끝나지 않습니다. 작업 계획, provider 선택, 실행 상태, 결과물과
인계 기록을 로컬에서 함께 관리합니다. Codex나 Claude Code가 종료되어도 실행 상태가 남기
때문에 긴 작업을 다시 이어갈 수 있습니다.

- 현재 버전: `2.4.1`
- Python: 3.11 이상
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

### 외부 전송을 확인하고 제어

저장소 파일이나 이전 결과물을 모델에 보낼 때는 포함할 파일, 데이터 크기, 목적 provider와
내용 digest를 먼저 만듭니다. 기본 설정에서는 연결 GUI에서 사람이 이 목록을 승인해야 합니다.
반복 작업에는 사용자 전역 `자동 승인` 스위치를 켤 수 있습니다. 자동 승인도 같은 digest,
policy revision과 일회용 review ID를 사용하며, 프로젝트의 전송 금지 정책과 민감 정보 보호를
우회하지 않습니다. 한 번 사용한 승인은 다시 사용할 수 없습니다.

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

`agent_hub_status`는 도구별 성공률과 소요 시간에 더해 실패 원인 코드 상위 항목을 함께
보여 줍니다. 실패가 정책상 의도된 차단인지 실제 오류인지 구분할 수 있습니다. 이 집계에는
고정된 코드 목록만 들어가며 prompt, 결과, 경로는 기록하지 않습니다. Step마다 사용한 입력과
출력 token도 남기므로 예산이 어디에서 소모됐는지 확인할 수 있습니다.

## 빠르게 시작하기

### 1. 설치

```bash
git clone https://github.com/Meapri/agent-hub.git
cd agent-hub
./scripts/bootstrap.sh
```

`bootstrap.sh`는 Python 3.11 이상을 찾아 `.venv`를 만들고 개발 의존성을 설치합니다.
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
2. 기본 설정에서는 연결 GUI에서 사용자가 파일 목록, 목적 provider, 크기와 digest를 검토해
   승인하거나 거부합니다. 전역 자동 승인이 켜져 있으면 허용된 요청은 이 단계에서 자동으로
   승인됩니다.
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
`outcome_unknown`이나 내부 오류는 이 경로로 재시도할 수 없습니다. `agent_hub_get`은
재시도 가능한 step ID와 최신 revision이 들어간 `next_action`을 함께 반환합니다.

### 멈춘 run을 다시 움직이는 방법

| 멈춘 이유 | 이어가는 방법 |
| --- | --- |
| 안전하게 재시도 가능한 step 실패 | `agent_hub_continue`에 `retry_failed_steps` |
| `run_token_budget_exhausted` | `agent_hub_continue`에 `token_budget_grant` |
| `outcome_unknown` | `agent_hub_cancel`의 `prepare_reconcile` / `apply_reconcile` |

토큰 예산은 계획에 봉인돼 있어 정책을 올려도 이미 만들어진 run에는 적용되지 않습니다. 그래서
계획의 예산은 그대로 두고 run 단위로 예산을 더하는 `token_budget_grant`를 사용합니다.
`agent_hub_get`이 남은 예산과 함께 권장 금액을 `next_action`에 담아 돌려줍니다.

`outcome_unknown`은 사람이 판단해야 풀립니다. `prepare_reconcile`로 step별 판정을 준비하고,
반환된 확인 문구와 digest를 그대로 넘겨 `apply_reconcile`로 적용합니다. 판정은 세 가지입니다.

| 판정 | 의미 | 결과 |
| --- | --- | --- |
| `not_delivered` | 외부 요청이 나가지 않았습니다 | step을 다시 대기열에 넣습니다 |
| `delivered_discarded` | 나갔지만 결과를 버립니다 | step을 실패로 확정합니다 |
| `delivered_recovered` | 나갔고 결과를 사람이 제공합니다 | 제공한 텍스트를 결과물로 저장합니다 |

외부 요청을 다시 보내는 판정은 `not_delivered` 하나뿐이고, 이때 확인 문구가 `resend-`로
시작해 무엇을 승인하는지 문구 자체에 드러납니다. `agent_hub_get`의 `next_action`은 언제나
재전송하지 않는 `delivered_discarded`만 미리 채워 두므로, 그 값을 그대로 실행해도 외부 요청이
다시 나가지 않습니다. 사람이 제공한 결과도 계획이 선언한 verifier를 통과해야 하며 출처가
`human_reconciliation`으로 남습니다.

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
| `agent_hub_cancel` | Run을 취소하거나, `outcome_unknown` run을 사람 판정으로 정리합니다. |
| `agent_hub_artifact` | 결과물 조회, 검증, 내보내기와 보관 정책을 다룹니다. |
| `agent_hub_feedback` | 평점과 채택·검증 결과를 기록합니다. |
| `agent_hub_policy` | 프로젝트 정책과 라우팅 prior를 조회하거나 prepare/apply 방식으로 변경합니다. |
| `agent_hub_handoff` | `HANDOFF.md` 갱신과 takeover를 관리하고, 적용 이력과 구간별 diff를 읽습니다. |
| `agent_hub_doctor` | 상태를 읽기 전용으로 진단하고 수리 계획을 만듭니다. |

Run 시작·진행·취소와 feedback은 `idempotency_key` 또는 `expected_revision`으로 경합을
막습니다. Policy, HANDOFF와 artifact 내보내기는 proposal digest와 대상 파일 SHA를 다시
확인합니다. 오류 응답에는 안전한 코드와 다음 행동만 포함하며, credential, 원본 prompt,
결과 본문과 raw exception은 event에 기록하지 않습니다.

## 라우팅과 프로젝트 정책

`routing_profile`은 후보를 비교하는 가중치를 정합니다. 세 가지를 지원합니다.

| Profile | 품질 | 신뢰성 | 지연시간 | Token 효율 |
| --- | ---: | ---: | ---: | ---: |
| `quality_balanced` (기본) | 60% | 20% | 10% | 10% |
| `latency_first` | 35% | 25% | 30% | 10% |
| `cost_first` | 35% | 20% | 5% | 40% |

`shadow`와 `advisory`는 planner가 고른 provider가 capability, 정책, 연결 상태와 context limit
검사를 통과하면 그 선택을 유지합니다. 실행할 수 없는 provider는 eligible fallback으로 바뀔 수
있습니다. `auto`는 충분한 표본과 품질 기준을 만족할 때만 provider 순서를 조정합니다. Mode는
자동으로 승격되지 않습니다.

### 라우팅 prior

실제 사용 표본이 쌓이기 전에는 통계가 provider를 구분하지 못합니다. `~/.agent-hub/routing_prior.toml`에
capability와 provider별 예상값을 적어 두면 관측값과 함께 계산합니다. 저장소에는 성능 수치가
하나도 들어 있지 않습니다. `agent_hub_policy`를 `target="routing_prior"`로 호출하면 모든 항목이
`source = "unset"`인 빈 서식을 만들어 주고, 이 상태의 항목은 가중치가 0이라 라우팅 결과가
바뀌지 않습니다. 값을 직접 채우고 `source`를 `user_estimate` 같은 값으로 바꿔야 반영됩니다.

Prior의 지분은 관측 표본이 쌓일수록 자동으로 줄어듭니다. 실제 실패가 몇 건만 기록돼도 낙관적인
prior는 곧바로 뒤집힙니다. `auto`가 provider를 바꾸려면 prior가 근거의 절반을 넘지 않아야 하고,
두 후보의 점수 차이가 각자의 불확실성보다 커야 합니다. 즉 예상값만으로는 provider가 바뀌지
않습니다. 어떤 근거로 선택했는지는 `routing_decision_v1`의 `evidence_kind`에 남습니다.
`collected_at`이 오래되면 prior 가중치가 서서히 줄고 `agent_hub_doctor`가 경고합니다.

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
max_output_tokens = 131072
max_total_tokens = 131072
# 필요할 때만 model context limit보다 작은 입력 상한을 지정합니다.
# max_input_tokens = 65536
```

`max_output_tokens`는 provider 한 번의 최대 출력, `max_total_tokens`는 run 전체 누적 사용량을
제한합니다. 선택 사항인 `max_input_tokens`는 model이 받을 context를 더 작게 제한할 때만
사용합니다. 이전 설정의 `max_tokens`는 output과 total의 호환 별칭으로 읽으며 input limit으로
사용하지 않습니다. Provider와 model 허용 목록, 외부 전송 규칙, 최대 실행 시간, provider
호출 횟수, token과 결과물 보관 방식을 프로젝트마다 다르게 설정할 수 있습니다.

## 보안과 데이터 경계

- 저장소 내용을 외부로 보내기 전 `egress_manifest_v2`를 만들고 일회용 review ID와
  digest·policy revision 재확인을 요구합니다. 사람의 개별 승인이 기본값이며, 연결 GUI의
  사용자 전역 스위치로 자동 승인을 선택할 수 있습니다.
- 전역 자동 승인은 프로젝트의 `denied` 정책, 민감 경로 차단, secret 제거를 우회하지 않으며
  자동 승인 여부와 당시 설정 revision을 review 기록에 남깁니다.
- Secret으로 보이는 줄은 fact pack과 로컬 색인에 넣기 전에 제외합니다.
- 긴 작업의 입력과 결과 artifact는 AES-GCM으로 암호화합니다.
- 암호화 key는 macOS Keychain에 저장합니다. SQLite DB 전체가 암호화되는 것은 아닙니다.
- 로컬 검색은 SQLite FTS5를 사용하며 기본 설정에서 cloud embedding을 호출하지 않습니다.
- Provider worker는 허용된 외부 도메인만 통과시키는 localhost proxy를 사용합니다.
- Provider config, cache와 credential 경로는 절대 경로이면서 HOME의 하위 디렉터리여야 합니다.
  `/`, HOME 자체, HOME의 상위 경로나 그곳을 가리키는 symlink는 worker 시작 전에 거부합니다.
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

`apply_update`가 성공하면 그때 적용된 관리 영역을 로컬 DB에 스냅샷으로 남깁니다. Git에 커밋되기
전에 인계 기록이 덮어써져도 `agent_hub_handoff`의 `history`로 이전 내용을 되찾을 수 있습니다.
`diff`는 스냅샷끼리, 또는 스냅샷과 현재 파일을 비교해 아홉 개 필수 구간 중 무엇이 바뀌었는지
보여 줍니다. 현재 파일과 비교하면 Agent Hub를 거치지 않고 수정된 내용도 드러납니다.
`prepare_update`에 `include_diff`를 주면 적용하기 전에 같은 비교를 미리 볼 수 있습니다.

스냅샷은 대상마다 최근 50개까지 보관하고 secret으로 보이는 줄은 저장 전에 제외합니다. 다만
Git 밖에 인계 본문의 사본이 생긴다는 점은 알아 두어야 합니다. 상태 DB는 `0600` 권한이지만
전체가 암호화되지는 않습니다.

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
./.venv/bin/python scripts/check_release_version.py
./.venv/bin/python -m build
```

사용자 문서는 다음 검사도 함께 실행합니다.

```bash
./.venv/bin/python -m orchestrate_codex.verify --user-facing README.md
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
