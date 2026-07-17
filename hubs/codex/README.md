# Agent Hub Codex 플러그인

Codex에서 Agent Hub의 통합 MCP, 공유 메모리, 작업 인계 스킬을 함께 사용하기 위한 로컬 플러그인입니다.

## 구성

```text
hubs/codex/
├── .codex-plugin/plugin.json
├── .mcp.json
└── skills/
    ├── adaptive-orchestrate/SKILL.md
    ├── handoff/SKILL.md
    ├── takeover/SKILL.md
    └── route-to/SKILL.md
```

`.mcp.json`에는 두 서버만 등록합니다.

- `agent-hub`: Claude, Grok, Gemini와 workflow를 제공하는 통합 MCP
- `memory`: Git으로 관리하는 로컬 basic-memory

provider별 MCP와 Orchestrator를 따로 설치하지 않습니다. 모델 호출은 모두 `agent_hub_*` 도구를 사용하며,
provider별 동의와 OAuth 검사는 Agent Hub 내부 adapter가 처리합니다.

복잡한 요청에는 `adaptive-orchestrate` 스킬이 `agent_hub_plan_workflow(workflow_id="adaptive")`로 planner
LLM의 DAG를 먼저 받고, 검토된 plan을 `agent_hub_run_workflow`로 실행합니다. 어떤 provider를 먼저 부를지
플러그인에 고정하지 않으며, DAG에서 의존성이 풀린 단계만 동시에 실행합니다.

## 준비

```bash
cd /Users/naen/Git/agent-hub-mono
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
uv tool install basic-memory
```

다른 위치에 clone했다면 `.mcp.json`의 절대경로를 실제 경로에 맞게 수정합니다.

```bash
codex plugin marketplace add /Users/naen/Git/agent-hub-mono
codex plugin add agent-hub@agent-hub
codex plugin list
```

## 확인

- `/mcp`에서 `agent-hub`와 `memory`가 연결되는지 확인합니다.
- `tools/list`에는 `agent_hub_*` 26개만 보여야 합니다.
- `orchestrate_*`, `claude_codex_*`, `grok_codex_*`, `google_antigravity_*`는 통합 서버에서 보이거나
  호출되면 안 됩니다.
- 모델 상태는 `agent_hub_status`, workflow 목록은 `agent_hub_list_workflows`로 확인합니다.
- adaptive smoke에서는 plan의 `schema=agent_hub_plan_v1`, 단일 final step, `plan_sha256`과
  `policy_sha256`, 1개 이상의 execution wave를 확인합니다.

## 원칙

- 작업 규칙과 상태의 정본은 Git에 둡니다.
- 모델을 반복 호출하면 provider 사용량이 소진될 수 있으므로 호출 범위를 먼저 정합니다.
- 민감한 인증 정보는 저장소에 기록하지 않습니다.
