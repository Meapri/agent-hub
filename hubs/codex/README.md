# agent-hub — Codex 플러그인

Codex를 agent-hub 콕핏으로 쓰기 위한 substrate 플러그인. Codex에 **공유 메모리(basic-memory)**와
**git-아티팩트 핸드오프 스킬**을 배선한다. 멀티모델 접근·오케스트레이션은 별도 published Codex 플러그인
4종(claude-codex / grok-codex / google-antigravity-codex / orchestrate-codex)이 담당한다.

> **역할**: BUILD-SPEC §0.0 재기획 기준, **Codex GUI가 콕핏**이고 지휘자는 **orchestrate-codex**(호스트 GPT가
> 판단)다. 이 substrate 플러그인은 메모리·핸드오프만 얹고, provider leaf와 conductor는 아래 4개 레포를
> 각각 설치해 쓴다. (orca 콕핏·PAL MCP는 은퇴 — BUILD-SPEC §0.0.)

## 구성
```
hubs/codex/
├── .codex-plugin/plugin.json   # 매니페스트
├── .mcp.json                   # MCP 서버 선언 (memory)
├── skills/handoff/SKILL.md      # /handoff — 현재 상태를 HANDOFF.md 복구 기록으로
├── skills/takeover/SKILL.md     # /takeover — HANDOFF.md 읽고 이어받기
└── skills/route-to/SKILL.md     # /route-to — provider leaf / orchestrate에 위임
```

## 사전 준비

1. **basic-memory** (공유 메모리): `uv tool install basic-memory` → MCP `uvx basic-memory mcp`.
   `.mcp.json`이 `BASIC_MEMORY_CONFIG_DIR`/`BASIC_MEMORY_HOME`로 노트를 repo `memory/data/`에 저장하고,
   `BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED=false`로 클라우드 임베딩을 차단한다(상세: `../../memory/README.md`).
2. **provider leaf + conductor 플러그인** (멀티모델 접근·오케스트레이션): 아래 4개 레포를 로컬에 클론한 뒤
   각각 `codex plugin marketplace add "<경로>"` → `codex plugin add <name>@<name>`로 설치한다. pinned commit·버전은
   `../../model-access/leaves.manifest.json` 참조.
   - `Meapri/claude-codex` (Claude), `Meapri/grok-codex` (Grok), `Meapri/google-antigravity-codex` (Gemini),
     `Meapri/orchestrate-codex` (conductor).
3. **인증**: 각 leaf는 **구독 OAuth + consent gate**를 스스로 강제한다(Claude Max/Pro, SuperGrok, Google).
   API 키가 필요한 leaf는 로컬 환경변수로만 두고 절대 커밋하지 않는다.

## 설치 (Codex)
1. Codex에서 `/plugins` 로 플러그인 디렉토리를 열거나, 개인 마켓플레이스(`~/.agents/plugins/marketplace.json`)에
   이 경로(`hubs/codex`)를 등록한다.
2. 활성화는 `~/.codex/config.toml`:
   ```toml
   [plugins."agent-hub@local"]
   enabled = true
   ```
3. 활성화 후 **새 스레드**를 시작해야 스킬·MCP가 로드된다. leaf/conductor 4종은 위 사전 준비에서 별도로 설치한다.

## 검증
- `codex mcp` (또는 `/mcp`)로 `memory` 서버가 뜨는지 확인.
- 스킬 3종(`handoff`/`takeover`/`route-to`)이 목록에 나오는지 확인.
- leaf/conductor는 `codex plugin list`로 설치 여부를 확인(예: `orchestrate-codex`, `claude-codex`).
- 실작동 증거는 `../../model-access/evidence/`의 orchestrate run 참조.

## ⚠️ 반드시 설치 직전에 확인할 것 (2026-07 스냅샷)
Codex 플러그인/MCP 스키마는 빠르게 바뀐다. 이 substrate 스캐폴드는 아직 실기 설치·검증 전이다. 재확인:
- `plugin.json` 스키마: `codex app-server generate-json-schema` 로 필드(`skills`/`mcpServers`/`interface`)를 검증.
- `.mcp.json` 위치·형식: 플러그인 루트에 두는지, bare object인지 `mcpServers` 래핑인지.
- `basic-memory`의 실제 실행 명령·env: `../../memory/README.md` 및 basic-memory 현재 버전으로 재확인.
- 마켓플레이스 등록 방식과 `[plugins."...@..."]` 키 형식. leaf 4종의 `codex plugin add` 인자 형식.

## 원칙 (BUILD-SPEC과 동일)
- 정본은 git, 도구(콕핏·플러그인)는 소모품. 상태를 콕핏 안에 저장하지 않는다.
- 지시(CLAUDE.md/AGENTS.md)는 이 플러그인이 아니라 각 프로젝트에서 Ruler가 생성 — 이 플러그인은 "역량"(메모리/핸드오프)만 얹는다.
- leaf 호출은 provider 과금/구독 상태 변경 — 대량 호출 전 목적을 분명히.
