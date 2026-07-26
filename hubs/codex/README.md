# Agent Hub v2 Codex 플러그인

Codex를 장기 실행되는 로컬 `agent-hubd`에 연결하는 플러그인입니다. MCP 프로세스는 얇은 Unix socket
bridge만 실행하며 Claude, Grok, Gemini, GPT 호출과 durable run은 daemon이 관리합니다.

## 구성

- `.codex-plugin/plugin.json`: 플러그인 메타데이터
- `skills/`: adaptive 계획, 문서 작성, provider 연결, handoff와 takeover 지침
- `skills/gpt-provider/SKILL.md`: GPT가 동일한 provider 계약을 사용한다는 경계
- `.mcp.json`: `agent-hub-mcp` bridge와 선택적 local memory 등록

provider별 MCP는 따로 등록하지 않습니다. 공개 표면은 `agent_hub_*` 14개이며 인증 변경은
`agent-hub-connect` GUI에서만 수행합니다.
`openai_codex_*`를 포함한 provider별 내부 도구는 공개 MCP에서 호출하지 않습니다.

## 준비

```bash
cd /absolute/path/to/agent-hub
./scripts/bootstrap.sh
./.venv/bin/agent-hub setup --repo-root . --json
./.venv/bin/agent-hub setup --repo-root . --apply --proposal-sha256 <REVIEWED_SHA>
```

setup은 먼저 host config와 LaunchAgent 계획을 보여 줍니다. 검토한 digest를 `--apply`에 넘긴 경우에만
변경합니다.

```bash
codex plugin marketplace add /absolute/path/to/agent-hub
codex plugin add agent-hub@agent-hub
codex plugin list
```

## 확인

- `agent_hub_status`에서 daemon, SQLite, 네 provider worker 상태를 확인합니다.
- `tools/list`에는 v2 `agent_hub_*` 14개만 보여야 합니다.
- `agent_hub_catalog`의 auth, catalog, generation 상태는 서로 다른 신호입니다.
- 저장소 자료를 보내는 계획은 `agent_hub_plan` prepare/apply를 거칩니다.
- durable run은 `agent_hub_start`, `agent_hub_continue`, `agent_hub_get`, `agent_hub_events`로 관리합니다.
- 결과 본문은 event가 아니라 encrypted `artifact_v2`로 전달됩니다.
