# Orchestrate Codex
프로바이더 중립 오케스트레이션 MCP 플러그인입니다. (버전 0.5.4)

## 개요
다양한 LLM 프로바이더(leaf)를 조율해 복잡한 문서 작성과 변환을 처리합니다. 오케스트레이터는 직접 판단을 내리지 않는 감독형(Supervised) 구조입니다. 대신 호스트 모델에게 작업 메뉴, 최신 모델 ID, 가드레일만 공급합니다. 실제 작업을 어떻게 나누고 실행할지는 전적으로 호스트 모델이 결정합니다.

## 동작 방식
MCP 서버는 형제 MCP를 직접 호출할 수 없습니다. 따라서 호스트가 지휘자 역할을 맡아 아래 흐름으로 작업을 진행합니다.
1. `advise`: 호스트에게 라우팅 정보 제공
2. 호스트의 배분 판단
3. `step`: 단일 leaf 호출 준비
4. `verify`: 결과물 검증

## 설치 및 연결
Python 3.9 이상에서 런타임 의존성 없이 동작합니다.

```bash
pip install -e .
pip install -e '.[dev]'
codex plugin marketplace add "/path/to/Orchestrate Codex"
```

MCP 서버 등록을 위한 `.mcp.json` 설정입니다.

```json
{
  "mcpServers": {
    "orchestrate-codex": {
      "command": "python3",
      "args": ["./scripts/orchestrate_codex_mcp.py"],
      "cwd": ".",
      "env": {
        "ORCHESTRATE_CODEX_RUNNING_UNDER_CODEX_MCP": "1"
      }
    }
  }
}
```

## MCP 도구 목록
| 도구 이름 | 설명 |
|---|---|
| orchestrate_list_recipes | 내장된 감독형 오케스트레이션 레시피 목록을 반환합니다. |
| orchestrate_explain_recipe | 레시피의 단계, 문서 클래스, 컨텍스트 정책, 기본 leaf 바인딩을 설명합니다. |
| orchestrate_context_policy | 문서 클래스(durable, change, transform, direct)별 컨텍스트 정책을 반환합니다. |
| orchestrate_plan_recipe | 단계와 제안 도구를 포함한 정적 계획을 생성합니다. |
| orchestrate_start_run | 감독형 실행을 시작하고 로컬 수집 단계를 자동 실행합니다. |
| orchestrate_continue_recipe | leaf 도구 실행 이후의 상태를 진행합니다. |
| orchestrate_get_run | run_id로 실행 상태를 조회합니다. |
| orchestrate_fallback_chains | 기본 기능에서 폴백 leaf 도구로 이어지는 체인을 보여줍니다. |
| orchestrate_advise | 호스트 모델에 라우팅 브리프와 최신 확인된 모델 ID를 제공합니다. |
| orchestrate_step | 계획된 단일 위임 leaf 호출을 준비하고 모델을 해석합니다. |
| orchestrate_verify | 생성된 텍스트의 환각 도구 사용, 세션 일기 톤, 가드레일을 검증합니다. |
| orchestrate_probe_models | 각 leaf별 최신 작동 모델 ID를 실시간으로 확인합니다. |
| orchestrate_run | 자율 브로커 모드로 레시피를 처음부터 끝까지 실행합니다. |
| orchestrate_check_leaves | 설정된 각 leaf를 스폰해 도구 목록을 확인하는 사전 점검을 수행합니다. |
| orchestrate_resolve_bindings | 현재 연결된 leaf 도구 이름을 기반으로 바인딩을 해석합니다. |

## 레시피 및 실행 상태
recipe는 MCP prompts로 노출합니다. run 상태는 `orchestrate://run/<id>` 형태의 resources로 노출해 추적할 수 있습니다.

## 레시피 도메인
* durable: `durable_readme`, `technical_doc`, `proposal`, `deep_readme`
* change: `change_pr`, `release_notes`, `review_diff`, `release_draft`
* transform: `translate_doc`, `polish_text`, `rewrite_text`, `summarize_text`, `research_brief`, `research_then_write`
* direct: `announcement`, `blog_post`, `email_draft`, `product_copy`, `generate_image`, `compare_models`, `direct_chat`

durable과 change 도메인의 초안 작업은 기본적으로 Antigravity `write` leaf로 보냅니다. 실패하면 chat으로 폴백합니다.

## 문서 클래스
| Class | Git | Session Diary | 용도 |
|---|---|---|---|
| durable | off | off | 안정적인 제품 문서(README, 기술 문서)용이며 최근 작업 톤을 배제합니다. |
| change | on | allowed | Git 변경 사항에 기반한 PR 설명 및 릴리스 노트용입니다. |
| transform | off | off | 기존 소스 텍스트의 다듬기, 번역, 요약에만 사용합니다. |
| direct | n/a | n/a | 단일 샷 leaf 호출용이며 다단계 오케스트레이션이 없습니다. |

## 자율 브로커
기본 동작은 감독형이지만 `orchestrate_run` 도구를 호출하면 자율 브로커 모드로 동작합니다. 브로커가 설정된 leaf MCP 서버를 서브프로세스로 스폰하고 stdio JSON-RPC 클라이언트가 됩니다. 동의(consent)와 인증(auth)은 각 leaf가 강제하므로 브로커가 임의로 우회할 수 없습니다.

leaf 실행 명령은 `~/.orchestrate_codex/leaves.json` 파일이나 `ORCHESTRATE_CODEX_LEAVES` 환경 변수에서 읽습니다. 에러는 auth, rate_limit, timeout, transient, bad_request로 분류합니다. 이 중 `bad_request`는 폴백 대상에서 제외합니다.

## 최신 모델 확인
정적 leaf 카탈로그는 금방 낡기 때문에 `orchestrate_probe_models` 도구로 실제 작동하는 모델을 핑(ping) 쳐서 실측합니다.

## 설정 파일 예시
`leaves.json`의 최소 설정 예시입니다.

```json
{
  "claude_codex_chat": {
    "command": "python3",
    "args": ["./scripts/claude_codex_mcp.py"],
    "cwd": "/abs/Claude Codex"
  },
  "google_antigravity": {
    "command": "python3",
    "args": ["./scripts/google_antigravity_mcp.py"],
    "cwd": "/abs/Antigravity Codex"
  }
}
```

## 개발
```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## 라이선스
MIT
