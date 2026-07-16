# agent-hub — Claude Code 플러그인

Claude Code를 agent-hub 콕핏의 한 축으로 쓰기 위한 플러그인. **공유 기반(substrate)을 Claude Code에 배선**한다:
공유 메모리(basic-memory), 멀티모델 접근(provider leaf 플러그인 + orchestrate-codex conductor),
git-아티팩트 핸드오프 스킬.

> **역할**: BUILD-SPEC §0.0 재기획 기준, 주 콕핏은 **Codex GUI + orchestrate-codex**이고 Claude Code는
> **동일한 stdio leaf를 재사용하는 2차 콕핏**이다(dual-hub, 추가 코드 없음). leaf/conductor MCP는 하네스
> 무관이라 Codex와 Claude Code가 같은 provider·핸드오프 규약·메모리를 공유한다. (orca·PAL은 은퇴.)

## 구성
```
hubs/claude-code/
├── .claude-plugin/plugin.json  # 매니페스트 (name=agent-hub)
├── .mcp.json                   # mcpServers: memory + claude_codex/grok_codex/antigravity(leaf) + orchestrate
└── skills/
    ├── handoff/SKILL.md         # /agent-hub:handoff — 상태를 HANDOFF.md 복구 기록으로
    ├── takeover/SKILL.md        # /agent-hub:takeover — HANDOFF.md 읽고 이어받기
    └── route-to/SKILL.md        # /agent-hub:route-to — provider leaf / orchestrate에 위임
```
컴포넌트는 기본 위치에서 자동 발견되므로 `plugin.json`에 경로 필드는 생략했다.

## 사전 준비
- **basic-memory**(공유 메모리): `uv tool install basic-memory` → MCP `uvx basic-memory mcp`. 로컬-퍼스트
  (평문 Markdown + SQLite). 저장 위치·클라우드 임베딩 차단은 Phase 3(EXECUTION-PLAN R4)에서 확정.
- **provider leaf 플러그인**(멀티모델 접근): 별도 published 레포 3종을 로컬에 클론
  (`~/Git/{Claude Codex, Grok Codex, Antigravity Codex}`). `.mcp.json`이 각 `scripts/*_mcp.py`를 stdio로 실행.
  각 leaf는 자기 consent gate + 구독 OAuth를 강제 — 최초 1회 로그인/동의 필요(사용자 행동).
- **orchestrate-codex**(다단계 conductor): `~/Git/Orchestrate Codex`. `.mcp.json`이 stdio로 실행.
- 실체 코드의 pinned commit·버전은 `../../model-access/leaves.manifest.json` 참조.

> `.mcp.json`의 leaf/orchestrate 경로는 **이 기기 기준 절대경로**다. 다른 기기에서 clone하면 경로를 수정한다.

## 설치 & 검증 (Claude Code v2.1.x)
```bash
claude plugin validate ./hubs/claude-code            # 구조·매니페스트 검증 (설치 없이)
claude --plugin-dir ./hubs/claude-code               # 이 세션에만 로컬 로드
/reload-plugins                                      # 편집 후 재로드
```
로드 후: `/agent-hub:handoff` 등 스킬과 `memory`/`claude_codex`/`grok_codex`/`antigravity`/`orchestrate`
MCP 서버가 붙는지 확인. leaf 최초 기동에 OAuth/consent가 필요할 수 있다.

## MCP 툴 네임스페이스
플러그인 MCP 툴은 `mcp__plugin_agent-hub_<server>__<tool>`로 네임스페이스된다
(예: `mcp__plugin_agent-hub_orchestrate__orchestrate_advise`). 실제 이름은 툴 목록에서 확인.

## 원칙 (BUILD-SPEC과 동일)
- 정본은 git, 콕핏·플러그인은 소모품. 상태를 도구 안에 저장하지 않는다.
- 지시(CLAUDE.md/AGENTS.md)는 이 플러그인이 아니라 각 프로젝트에서 Ruler가 생성 — 플러그인은 "역량"만 얹는다.
- leaf 호출은 provider 과금/구독 상태 변경 — 대량 호출 전 목적을 분명히.
