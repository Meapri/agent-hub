# Agent Hub v2 Claude Code 플러그인

Claude Code를 장기 실행되는 로컬 `agent-hubd`에 연결하는 플러그인입니다. MCP 프로세스는 얇은 Unix
socket bridge만 담당하고 Claude, Grok, Gemini, GPT 호출과 durable run은 daemon에서 계속 실행됩니다.

## 구성

- `.claude-plugin/plugin.json`: 플러그인 메타데이터
- `commands/`: egress prepare/apply 계획과 durable run 명령
- `skills/`: adaptive 계획, 문서 작성, provider 연결, handoff와 takeover 지침
- `skills/gpt-provider/SKILL.md`: GPT가 동일한 provider 계약을 사용한다는 경계
- `.mcp.json`: `agent-hub-mcp` bridge와 선택적 local memory 등록

공개 표면은 provider와 관계없이 `agent_hub_*` 14개입니다. provider별 내부 adapter는 별도 MCP로
노출하지 않으며 인증 변경은 `agent-hub-connect` GUI에서만 수행합니다.
`openai_codex_*`를 포함한 내부 도구 이름은 host 설정이나 스킬에서 직접 사용하지 않습니다.

## 준비

```bash
cd /absolute/path/to/agent-hub
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
./.venv/bin/agent-hub setup --repo-root . --json
./.venv/bin/agent-hub setup --repo-root . --apply --proposal-sha256 <REVIEWED_SHA>
```

setup은 host config와 LaunchAgent 계획을 먼저 보여 주며 검토한 digest 없이는 적용하지 않습니다.

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

- `tools/list`에는 v2 `agent_hub_*` 14개만 보여야 합니다.
- `/agent-hub-plan`은 egress를 준비하고 승인된 proposal로 `plan_v2`를 만듭니다.
- `/agent-hub-run`은 idempotent run을 만들고 revision-fenced continue로 진행합니다.
- `agent_hub_catalog`의 auth, catalog, generation 상태를 따로 확인합니다.
- 결과 본문은 encrypted `artifact_v2`, 진행 정보는 redacted `event_v2`에서 읽습니다.

이전 v1 도구는 임시 호환 entrypoint `agent-hub-v1-mcp`에 남아 있습니다.
