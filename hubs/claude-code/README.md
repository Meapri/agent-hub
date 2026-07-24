# Agent Hub Claude Code 플러그인

Claude Code에서 Agent Hub의 통합 MCP, 공유 메모리, 작업 인계 스킬을 함께 사용하기 위한 로컬 플러그인입니다.

## 구성

- `hubs/claude-code/.claude-plugin/plugin.json`은 플러그인 메타데이터입니다.
- `hubs/claude-code/commands/`에는 adaptive plan과 run 명령이 있습니다.
- `hubs/claude-code/skills/`에는 adaptive orchestration, 문서 작성, GPT 연결, handoff, takeover,
  provider 위임 스킬이 있습니다.
- GPT가 별도 MCP가 아니라는 계약은 `hubs/claude-code/skills/gpt-provider/SKILL.md`에 있습니다.
- `agent-hub-setup`이 만드는 machine-local MCP 설정에는 아래 두 서버만 등록합니다.

- `agent-hub`: Claude, Grok, Gemini, GPT와 workflow를 제공하는 통합 MCP
- `memory`: Git으로 관리하는 로컬 basic-memory

provider별 MCP와 Orchestrator를 따로 등록하지 않습니다. 모델 호출은 모두 `agent_hub_*` 도구를 사용하며,
provider별 동의와 OAuth 검사는 Agent Hub 내부 adapter가 처리합니다.

`/agent-hub-plan <목표>`는 planner LLM이 만든 검증된 DAG만 보여 주고, `/agent-hub-run <목표>`는 그 plan을
의존성 기반으로 실행합니다. 어떤 provider를 먼저 부를지 플러그인에 고정하지 않으며, DAG에서 의존성이
풀린 단계만 동시에 실행합니다.

## 준비

```bash
cd /absolute/path/to/agent-hub
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
uv tool install basic-memory
./.venv/bin/agent-hub-setup --apply
```

`agent-hub-setup`은 현재 clone 경로를 기준으로 로컬 MCP 설정을 렌더링합니다.

```bash
claude plugin marketplace add /absolute/path/to/agent-hub
claude plugin install agent-hub@agent-hub --scope user
claude plugin list
```

## 실행과 확인

```bash
claude plugin validate ./hubs/claude-code
claude --plugin-dir ./hubs/claude-code
```

- MCP 목록에서 `agent-hub`와 `memory`가 연결되는지 확인합니다.
- `tools/list`에는 `agent_hub_*` 37개만 보여야 합니다.
- `orchestrate_*`, `claude_codex_*`, `grok_codex_*`, `google_antigravity_*`,
  `openai_codex_*`는 통합 서버에서 보이거나 호출되면 안 됩니다.
- 모델 상태는 `agent_hub_status`, workflow 목록은 `agent_hub_list_workflows`로 확인합니다.
- adaptive smoke에서는 plan의 `schema=agent_hub_plan_v1`, 단일 final step, `plan_sha256`과
  `policy_sha256`, 1개 이상의 execution wave를 확인합니다.

## 원칙

- 작업 규칙과 상태의 정본은 Git에 둡니다.
- 모델을 반복 호출하면 provider 사용량이 소진될 수 있으므로 호출 범위를 먼저 정합니다.
- 민감한 인증 정보는 저장소에 기록하지 않습니다.
