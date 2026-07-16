# EXECUTION-PLAN — agent-hub 완성 실행 계획 (2026-07-16 재기획 확정판)

> **이 문서의 목적**: 이어받는 에이전트(어느 하네스든, 원 대화 컨텍스트 없이)가 agent-hub를
> "완성" 상태까지 끌고 가기 위한 **실행 runbook**이다. 설계 근거는 `BUILD-SPEC.md` §0.0,
> 살아있는 진행 상태는 `HANDOFF.md`에 있다. 세 문서가 충돌하면 **HANDOFF.md(최신 상태) >
> 이 문서(실행 절차) > BUILD-SPEC.md(설계 근거)** 순으로 읽는다.
>
> 읽는 순서: ① `HANDOFF.md` 최상단 재기획 블록 → ② 이 문서 전체 → ③ 필요 시 BUILD-SPEC §0.0.
>
> 이 확정판은 3각도 적대적 검증(콜드스타트 실행가능성 / 디스크 사실 대조 / 안전·경계)을 거쳐
> 15건의 발견을 반영했다(2026-07-16).

---

## 1. 목표와 "완성"의 정의

여러 AI 코딩 하네스를 오가며 **작업 플로우가 끊기지 않는 개인용 시스템**을 만든다.
모든 상태는 git 커밋 파일에 산다. 도구는 소모품이다.

**"완성" = 아래 3개의 커밋된 승인 증거가 agent-hub에 존재하는 상태.**

| # | 증거 | 판정 파일 | 상태 |
|---|---|---|---|
| E1 | 모델접근·오케스트레이션: leaf 매니페스트 + 실제 orchestrate run 트랜스크립트 | `model-access/leaves.manifest.json`, `model-access/evidence/` | ✅ `3527245` |
| E2 | 핸드오프: Claude Code ↔ Codex 실작업 왕복 1회 기록 | `handoff/HANDOFF-e2-*.md` + 해당 커밋들 | ✅ 2026-07-16 |
| E3 | 메모리: 하네스 A에서 쓴 메모리를 하네스 B에서 읽은 기록 | `memory/data/` + `HANDOFF.md` 검증 기록 | ✅ 2026-07-16 |

> **E2 검증(2026-07-16, 양방향)**:
> ① **Claude Code → Codex**: `handoff/HANDOFF-e2-demo.md` 패킷으로 `scripts/doctor.sh`의 `[5/5]` 검사를
> Claude가 절반(라벨/placeholder) → Codex(`codex exec -s workspace-write`)가 원 대화 없이 패킷만 읽고 구현.
> 실행 중 codex 샌드박스가 sync를 중단해 남긴 stale `.ruler` bridge를 문서 절차대로 제거 후 `doctor: OK`(5/5 PASS).
> ② **Codex → Claude Code**: Codex가 `handoff/HANDOFF-e2-reverse.md` 패킷을 작성 → Claude가 그 패킷만 보고
> 이 R4 설명(검사 4개→5개)을 수정. 어느 방향도 이어받은 쪽이 원 대화 없이 "다음 한 걸음"을 완수했다.

> **E3 검증(2026-07-16)**: `memory/data/decisions/agent-hub replan 2026-07-16.md`를 basic-memory로 저장한 뒤,
> **Claude Code**(`claude -p --mcp-config <memory-only>`)와 **Codex**(`codex exec -c mcp_servers.memory...`,
> ChatGPT 로그인)가 각각 memory MCP `read_note`로 동일 노트를 읽어 `CROSS_HARNESS_OK`를 출력했다
> (codex 로그: `mcp: memory/read_note completed`). 두 하네스가 같은 로컬 store를 공유함을 실증.

E1~E3가 모두 커밋되면 이 프로젝트는 완성이다. 이후는 운영/개선이지 구축이 아니다.

---

## 2. 검증된 현재 상태 스냅샷 (2026-07-16 15:30 KST)

집행 전 이 스냅샷이 아직 유효한지 `git log`/`ls`로 재확인하라. 어긋나면 HANDOFF.md를 먼저 갱신.
이 레포는 **병렬 세션이 실제로 동시 커밋**하므로(2026-07-16 실측) 작업 시작·커밋 직전에
`git status`/`git log -3`를 반드시 재확인한다.

### 2.1 확정 아키텍처 (BUILD-SPEC §0.0)

```
        [ Codex GUI = 콕핏 ]  (지휘 판단 = 호스트 GPT)
                 │  orchestrate-codex  ← conductor MCP (advise/step/verify + opt-in broker)
     ┌───────────┼────────────────┬─────────────────┐
 claude-codex   grok-codex   antigravity-codex        ← provider leaf MCP (direct-HTTP, 구독 OAuth, consent gate)
   ┌──────────────────────────────────────────────┐
   │ 공유 기반(git): Ruler 지시 ✅ · HANDOFF 규약 · basic-memory(예정)
   └──────────────────────────────────────────────┘
```

- 은퇴 확정: orca 콕핏, PAL MCP, LiteLLM/OpenRouter 게이트웨이, 로컬 gemma/qwen 라우터·coordinator.
- 로컬 라우터 연구는 **종료·보존**: `model-access/policy/gemma4-e4b-router/RESEARCH-CLOSURE.md`
  (기준 커밋 `9d37b59`, 종료 커밋 `90510db`). frontier-v4 holdout 64는 **미개봉 유지**.

### 2.2 외부 레포 4종 (실체 코드 위치)

| 이름 | 역할 | 로컬 경로 | GitHub | 로컬 HEAD | published | 버전 |
|---|---|---|---|---|---|---|
| orchestrate-codex | conductor | `~/Git/Orchestrate Codex` | `Meapri/orchestrate-codex` | `cce4df4` | `cce4df4` (**synced, pushed**) | 0.5.3 |
| claude-codex | leaf(Anthropic) | `~/Git/Claude Codex` | `Meapri/claude-codex` | `9268c93` | 동일 | 0.2.0 |
| grok-codex | leaf(xAI) | `~/Git/Grok Codex` | `Meapri/grok-codex` | `14d7bc3` | 동일 | 0.2.0 |
| antigravity-codex | leaf(Google) | `~/Git/Antigravity Codex` | `Meapri/google-antigravity-codex` | `4355b37` | 동일 | 0.9.8 |

- 로컬 경로에 **공백이 포함**된다 — 셸/JSON에서 항상 인용.
- MCP entrypoint(전부 stdio): conductor `scripts/orchestrate_codex_mcp.py`, leaf 각각
  `scripts/claude_codex_mcp.py` / `scripts/grok_codex_mcp.py` / `scripts/google_antigravity_mcp.py`.
- **orchestrate-codex는 2026-07-16 push 완료** (HEAD==origin `cce4df4`, v0.5.3). R0-1 해소됨. 이후
  버전이 오르면(예: v0.5.2→v0.5.3처럼) `leaves.manifest.json` pin을 실측으로 갱신하고 doctor.sh 검사 3으로 drift를 잡는다.

### 2.3 작동 증거 (라이브 검증 완료된 것)

- `~/.orchestrate_codex/runs/`에 run 7개(2026-07-16 14:07~14:33, 전부 version 0.4.0;
  **status=completed 4개, failed 1개, running(중단 잔류) 2개**).
  핵심 증거: `970c19b355f8.json` — recipe `deep_readme`, `status=completed`,
  `claude_codex_chat=completed; grok_codex_chat=completed; google_antigravity_write=completed`, verify 통과.
- 배선: `~/.orchestrate_codex/leaves.json`이 세 leaf의 로컬 경로를 가리킴 (off-git — R1에서 편입).
- secret 사전 점검(2026-07-16 실측): R1의 스캔 정규식 기준 `970c19b355f8.json` **매치 0건**.
  파일 내 `sk-ant-...` 유사 텍스트는 전부 생성된 README 초안 속 플레이스홀더라 정규식에 걸리지 않는다.
  → R1 스캔에서 **매치 0(= grep exit 1)이 정상·통과**이며, 매치가 나오면 그때가 redaction 대상이다.

### 2.4 라이브 검증 안 된 것 (주장만 있는 것)

- claude-codex **구독 plan-lane 과금**: live 호출은 있었지만 구독 할당 소진인지 종량 API 과금인지 미확인 (R3).
- antigravity-codex image gen / grounded search live 동작: author 주장. 완료 run이 실증한 capability는 chat/write뿐.
- grok-codex의 MCP `grok_codex_login_*` 툴은 정의만 있고 dispatch 테이블에 없어 MCP 경유 호출 불가
  (CLI `scripts/grok_codex_login.py`는 동작). chat은 live 검증됨.
- `hubs/claude-code`·`hubs/codex` 스캐폴드: JSON/manifest 검증만 통과. 그 안의 `pal` MCP 엔트리는 은퇴 대상 (R6).

---

## 3. 실행 단계 R0~R6

각 단계는 독립 커밋으로 남기고, 완료 시 HANDOFF.md의 해당 항목을 갱신해 같은 커밋에 포함한다.
단계 안에서 이 문서와 현실이 어긋나면 **현실 우선 + HANDOFF에 차이 기록**.
🔒 = 사용자 행동/승인 필요. **사용자 미실행 상태에서 해당 Acceptance를 체크하지 않는다**
(실행 못 한 검증을 통과로 쓰지 않는다 — CLAUDE.md).

### R0. 사용자 게이트 요청 목록 전달 — 🔒 사용자 행동 필요

에이전트는 아래를 **직접 실행하지 말고**, 사용자에게 요청 목록으로 전달한다:

1. ~~`~/Git/Orchestrate Codex`에서 `git push origin main`~~ — ✅ 완료(2026-07-16, `cce4df4`/v0.5.3 published).
2. R3 과금 확인: claude_codex_chat 1회 호출 승인 + Anthropic Console 사용량 확인(계정 UI라 에이전트 불가).
3. R6 등에서 leaf 최초 기동 시 OAuth 로그인/consent 부여가 필요하면 사용자가 수행.

사용자가 거부/보류해도 R1~R2, R4 전반부는 진행 가능하다.

### R1. Custody — 작동 시스템을 agent-hub 정본에 편입

**목적**: E1 증거 확보. "작동하지만 git 밖" 상태 해소.

**선행 확인**: 재기획 문서 세트(EXECUTION-PLAN.md, BUILD-SPEC §0.0, HANDOFF 재기획 블록, README)가
커밋되어 있는지 `git log --oneline -5`로 확인. 미커밋 상태로 발견되면 **docs 커밋을 먼저** 만든다
(무관 diff와 섞지 않는다).

**작업**:
1. `model-access/leaves.manifest.json` 생성. 스키마(conductor·leaves 모두 동일 필드셋 사용):
   ```json
   {
     "updated": "<ISO date>",
     "cockpit": "codex-gui",
     "conductor": { "name": "orchestrate-codex", "repo": "https://github.com/Meapri/orchestrate-codex",
       "local_path": "~/Git/Orchestrate Codex", "pinned_commit": "<git rev-parse HEAD>",
       "published_commit": "<git rev-parse origin/main>", "version": "<pyproject>",
       "entrypoint": "scripts/orchestrate_codex_mcp.py", "verified_live": true },
     "leaves": [ { "name": "...", "provider": "...", "repo": "...", "local_path": "...",
       "pinned_commit": "...", "published_commit": "...", "version": "...", "entrypoint": "...",
       "auth": "subscription-oauth|api-key", "consent_gate": true,
       "verified_live_capabilities": ["chat"] } ],
     "wiring": { "leaves_json": "~/.orchestrate_codex/leaves.json",
       "note": "off-git 런타임 배선. 사본은 evidence/ 참조" }
   }
   ```
   pinned/published_commit은 **작성 시점에 실측**(`git -C "<path>" rev-parse HEAD` / `origin/main`).
   §2.2 표는 참고값 — 어긋나면 실측이 우선.
2. `model-access/evidence/` 생성:
   - `orchestrate-run-970c19b355f8.json` — `~/.orchestrate_codex/runs/970c19b355f8.json` 사본.
   - `leaves.json` — `~/.orchestrate_codex/leaves.json` 사본.
   - `EVIDENCE.md` — 이 run이 무엇을 증명하는지 3~5줄(레시피, 참여 leaf, 완료 상태, 날짜, version 0.4.0)
     + 아래 스캔·검토 결과 기록.
3. 커밋 전 **secret 스캔**(필수):
   ```bash
   grep -inE 'sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xai-[A-Za-z0-9]{8,}|Bearer[[:space:]]+[A-Za-z0-9._-]{16,}|-----BEGIN|"(api_key|access_token|refresh_token)"[[:space:]]*:[[:space:]]*"[^$"]' \
     model-access/evidence/orchestrate-run-970c19b355f8.json model-access/evidence/leaves.json
   ```
   **매치 0(exit 1)이 통과.** 매치가 나오면 해당 값을 `"[REDACTED]"`로 치환하고 EVIDENCE.md에 명시.
4. 커밋 전 **내용 검토**(필수, secret과 별개): run JSON의 생성 본문(README 초안 등)이 비공개
   프로젝트 내용을 담는지 **직접 읽고 판단**한다. 민감하면 해당 본문 필드를 `"[TRUNCATED]"`로 치환하고
   EVIDENCE.md에 치환 사실을 기록. (참고: 이 run의 본문은 agent-hub 계열 공개 문서 초안으로 확인됨,
   2026-07-16 — 그래도 커밋 시점에 재확인.)

**Acceptance**:
- [ ] `python3 -m json.tool model-access/leaves.manifest.json` 통과.
- [ ] manifest의 pinned_commit 4개가 각 로컬 레포 `git rev-parse HEAD` 실측과 일치.
- [ ] secret 스캔 exit 1(매치 0) 또는 redaction 완료 — 결과를 EVIDENCE.md에 기록.
- [ ] 내용 검토 수행 기록이 EVIDENCE.md에 있음.
- [ ] 커밋 1개: `feat: register leaf plugins and orchestrate run evidence (E1)`.

### R2. HANDOFF 잔여 stale 서술 정리 — ✅ 완료 (2026-07-16, 재기획 세션)

**완료 내용**: `HANDOFF.md` 미완 섹션의 게이트웨이 줄 은퇴 표기, Phase 5 잔여에 pal 은퇴 주석,
orca 항목 은퇴 표기, 각 항목 R-단계 참조 부여. 이어받는 에이전트는 편집하지 말고 **검증만**:
```bash
grep -inE 'litellm|pal|orca' HANDOFF.md
```
매치가 전부 ①취소선(`~~`)/"은퇴" 문맥, ②`[2026-07-15 기록...]` 역사 블록 내부, ③검증·변경파일
히스토리 기록 중 하나에 속하면 통과. 새로 "미완 작업"으로 서술된 매치가 있으면 그때만 수정.

### R3. claude-codex 과금·ToS 확인 — 🔒 전체 사용자 게이트

**목적**: 이미 live 호출이 발생한 구독 OAuth 경로가 실제로 구독 할당(plan lane)으로 과금되는지 확인.
미확인 상태로 대량 사용하면 조용한 종량 과금 위험. + 'Claude Code fingerprint' 사칭의 ToS 리스크를
사용자가 **명시적으로 수용/거부**하도록 기록.

**작업** (호출 발생 단계는 모두 🔒 — 과금 상태 변경이므로 사용자 승인 없이 실행 금지):
1. (에이전트 가능) `cd "$HOME/Git/Claude Codex" && python3 scripts/claude_codex_doctor.py`로
   auth mode·consent 상태 확인. consent 미부여면 사용자에게 부여 여부를 묻는다(🔒).
2. 🔒 (사용자 승인 후 1회만) 시각을 기록하고 `claude_codex_chat`을 짧은 프롬프트로 1회 호출.
   호출 경로는 둘 중 하나를 사용자와 합의:
   - (a) 사용자가 Codex GUI에서 직접 1회 호출, 또는
   - (b) 에이전트가 일회성 headless 세션으로 호출:
     `claude -p --mcp-config '{"mcpServers":{"claude_codex":{"command":"python3","args":["/Users/naen/Git/Claude Codex/scripts/claude_codex_mcp.py"]}}}' "claude_codex_chat 툴로 'ping'이라고 한 번만 물어보고 결과를 요약해."`
3. 🔒 사용자: Anthropic Console 사용량에서 해당 시각의 API 과금 발생 여부 확인.
   - API 과금 없음 + 구독 사용량 증가 → plan-lane 동작 확인.
   - API 과금 발생 → fingerprint 무효. `CLAUDE_CODEX_AUTH_MODE=api_key`를 기본으로 전환 권고.
4. 결과와 사용자의 ToS 리스크 수용 여부를 `model-access/evidence/EVIDENCE.md`에 1줄 기록.

**Acceptance**: 과금 경로 판정 + 사용자 결정이 커밋된 문서에 기록됨. (판정 전까지 자동화·대량 호출 금지.
사용자 미실행 시 이 단계는 미완으로 남긴다.)

### R4. Phase 3 — 메모리 배선 (E3)

**결정(확정)**: 저장소는 레포 내 `memory/data/`(git 추적). 이유: state-in-git 원칙, BUILD-SPEC §4.2와 일치.

**작업**:
1. `memory/data/.gitkeep` + `memory/README.md`(기동·등록법, 임베딩 정책) 생성.
2. `instructions/.ruler/ruler.toml`의 `[mcp_servers.memory]` 주석 해제. 프로젝트 디렉토리 지정은
   basic-memory v0.22.1(2026-07-15 실기 확인 버전) 기준으로 구성하되, 현행 버전의 지정 방식
   (`--project` 인자 또는 `BASIC_MEMORY_*` env)을 `uvx basic-memory --help`로 **로컬에서 확인**한다.
   확인 실패 시 v0.22.1 문서 기준값으로 진행하고 차이를 HANDOFF에 기록(외부 웹 접근 없이 진행 가능해야 함).
3. **클라우드 임베딩 차단 확인(필수)**: basic-memory는 `openai`/`litellm`을 의존성으로 끌어온다.
   `uvx basic-memory --help`/설정 파일에서 임베딩·클라우드 관련 옵션을 확인해 풀텍스트/로컬 모드로
   명시 고정한다. 해당 옵션이 없으면: 환경에 `OPENAI_API_KEY` 등 클라우드 키가 unset인 것을 확인하고
   그 사실과 잔여 위험을 `memory/README.md`에 기록한 뒤 진행.
4. `./scripts/sync.sh` → `./scripts/check-sync.sh` 로 두 허브 설정에 전파.
5. `scripts/doctor.sh` 신설 — 최소 검사 5개:
   Ruler 정합(`check-sync.sh` 위임) / basic-memory 기동(`uvx basic-memory --version`) /
   `model-access/leaves.manifest.json` pinned_commit vs 로컬 레포 HEAD drift / `~/.orchestrate_codex/leaves.json` 존재 /
   `memory/data` 노트 store 비어있지 않음.
6. **E3 검증** — 실행 주체를 명시한 두 경로 중 하나:
   - (a) 에이전트 단독(headless): 쓰기 = `claude -p --mcp-config '<memory 서버 등록 JSON>' "basic-memory에 테스트 노트 1건 작성"`,
     읽기 = `codex exec "basic-memory MCP에서 방금 노트를 읽어 내용을 출력"` (codex CLI 설치·로그인 필요.
     미설치면 (b)로).
   - (b) 🔒 사용자 보조: 사용자가 Codex GUI 세션에서 읽기를 수행하고 출력 사본을 전달 → HANDOFF에 기록.
   어느 경로든 실제 명령/출력 요약을 HANDOFF.md 검증 실행 결과에 기록. **상대 하네스 측이 미실행이면
   E3를 체크하지 않는다.**

**Acceptance**:
- [ ] `memory/data/`에 사람이 읽을 수 있는 Markdown 1건 이상 커밋.
- [ ] 크로스-하네스 read 실증 기록(실행 주체·명령·출력 요약 포함).
- [ ] `scripts/doctor.sh` exit 0, 클라우드 임베딩 off(또는 키 unset 확인) 기록.

### R5. Phase 2 — 크로스-허브 핸드오프 실연 (E2)

**작업**:
1. `handoff/HANDOFF.template.md` 생성(BUILD-SPEC §3.3 템플릿 그대로).
2. **왕복 리허설 fixture**: 실제 소규모 작업 1개를 반으로 나눠 실행.
   권장 fixture: "doctor.sh에 검사 2개 추가" — 전반(검사 1개)을 Claude Code에서 구현·커밋하고
   HANDOFF 패킷 작성 → **Codex 측에서 원 대화 없이** 패킷만 읽고 후반(검사 1개) 완성.
   Codex 측 실행은 `codex exec`(에이전트 가능 시) 또는 🔒 사용자가 Codex GUI에서 수행.
   역방향: 후속 미세 작업(예: README doctor 섹션)을 Codex에서 시작 → Claude Code에서 완료.
3. 각 방향에서 이어받은 쪽이 "다음 한 걸음"만으로 실행 가능했는지, 막힌 지점을 HANDOFF.md에 기록.
4. 순서 유연성: hubs 스킬(`/handoff`·`/takeover`)의 실사용은 R6(재배선) 이후에만 가능하므로,
   R5 시점에는 **동등 절차**(HANDOFF.template 수동 작성/판독)로 충분하다. 스킬 실사용 검증을 원하면
   R6 완료 후 재실행한다.

**Acceptance** (기계 판정 가능형):
- [ ] HANDOFF.md에 양방향 각 1회의 기록이 있음 — 각 기록에 실행 하네스, 관련 커밋 해시,
      사용한 절차명(스킬 또는 "HANDOFF.template 동등 절차")이 명시됨.
- [ ] 이어받은 쪽 실행이 실제로 일어났음(🔒 미실행 상태로 체크 금지).

### R6. Phase 5 — hubs/ 재배선 + dual-hub 범위 확정

**결정(확정)**: **Codex-primary, Claude Code는 substrate 소비자 + 동일 leaf의 2차 콕핏.**
근거: 출하물이 전부 Codex 플러그인이지만, leaf들은 하네스 무관 **stdio MCP 서버**라서
Claude Code `.mcp.json`에도 그대로 등록 가능 — 새 코드 없이 dual-hub가 성립한다.

**작업**:
1. `hubs/claude-code/.mcp.json`: `pal` 제거 → 세 leaf + orchestrate + memory 등록.
   **규약 확정: 기기 종속 절대경로 + README 주석**("경로는 이 기기 기준, clone 시 수정").
   예시(공백 경로 주의 — args 배열 원소로 통째 인용):
   ```json
   {
     "mcpServers": {
       "claude_codex":   { "command": "python3", "args": ["/Users/naen/Git/Claude Codex/scripts/claude_codex_mcp.py"] },
       "grok_codex":     { "command": "python3", "args": ["/Users/naen/Git/Grok Codex/scripts/grok_codex_mcp.py"] },
       "antigravity":    { "command": "python3", "args": ["/Users/naen/Git/Antigravity Codex/scripts/google_antigravity_mcp.py"] },
       "orchestrate":    { "command": "python3", "args": ["/Users/naen/Git/Orchestrate Codex/scripts/orchestrate_codex_mcp.py"] },
       "memory":         { "command": "uvx", "args": ["basic-memory", "mcp"] }
     }
   }
   ```
   `claude plugin validate ./hubs/claude-code` 재통과 확인.
2. `hubs/codex/`: 실제 설치는 각 레포에서 `codex plugin marketplace add`로 하므로, 스캐폴드는
   **설치 절차 문서**로 전환(README에 4개 레포 설치 커맨드 나열)하고 `.mcp.json`의 pal 제거.
3. 라이브 로드 검증 — 범위 제한:
   - 등록은 **레포 내 파일**(`hubs/claude-code/.mcp.json` 또는 프로젝트 `.mcp.json`)로만 한다.
     `~/.claude.json` 등 **사용자 레벨 설정 쓰기는 🔒**.
   - 검증은 일회성 headless 세션으로: `claude -p --mcp-config` 에 antigravity-codex entrypoint를 넘겨
     `google_antigravity_list_models` 1회 → 출력 요약을 HANDOFF에 기록.
   - leaf 최초 기동에 OAuth 로그인/consent가 필요하면 🔒 사용자 수행(R0 목록 3).
   - `list_models`가 live provider 호출인 경우 R3 판정 전에는 **호출 최소화**(1회 한정).
4. 스코어카드 1단락: 어떤 작업을 어느 콕핏에서(BUILD-SPEC §6.1 세 번째 Acceptance).

**Acceptance**:
- [ ] `grep -rinE 'pal|orca' hubs/` 매치가 0이거나 전부 역사 주석.
- [ ] `claude plugin validate ./hubs/claude-code` 통과.
- [ ] Claude Code 측 leaf 1개 라이브 호출 성공 기록(🔒 요건 충족 시에만 체크).

---

## 4. 단계 의존성

```
R0(사용자) ─── 독립, 언제든
R1 custody ──→ R4 메모리 ──→ R5 핸드오프 ──→ R6 hubs 재배선
R3 과금확인 ── R1 이후 아무 때나 (단 대량 사용 전 필수; R6의 live 호출과도 연동)
R2 ── 완료됨(검증만)
```
R4·R5 순서는 바꿔도 되나, doctor.sh(R4)가 R5 fixture의 재료이므로 이 순서가 경제적이다.
R5의 "스킬 실사용" 요건은 R6 이후로 미룰 수 있다(R5 작업 4).

---

## 5. 경계 (Opus 절대 금지 — CLAUDE.md 절대 금지에 추가)

1. **원격 push 금지** — agent-hub 포함 모든 레포. R0의 orchestrate push도 사용자가 직접 한다.
2. **외부 레포 4종은 읽기 전용** — R3의 doctor 실행은 허용, 파일 수정·커밋은 해당 레포
   작업을 사용자가 명시 요청할 때만.
3. **라우터 연구 재개 금지** — `RESEARCH-CLOSURE.md`의 재개 조건 + 사용자 명시 요청 전에는
   trajectory 수집·튜닝·calibration·holdout 채점 일체 금지. **frontier-v4 holdout 64 개봉 금지.**
4. **Ruler 생성물 직접 편집 금지** — `CLAUDE.md`/`AGENTS.md`/`.gemini/settings.json`은
   `instructions/.ruler/` 수정 후 `sync.sh`로만.
5. **실키·토큰 커밋 금지** — evidence 파일은 R1 스캔+내용검토 절차 통과 후에만.
6. **한 작업 디렉토리 한 writer** — 이 레포는 병렬 세션이 실제로 커밋한다(2026-07-16 실측:
   세션 도중 `9d37b59`/`90510db` 유입). 작업 시작·커밋 직전 `git status`/`git log -3` 재확인.
7. **live provider 호출·OAuth 동의·사용자 레벨 설정 쓰기 = 과금/권한 상태 변경으로 취급** —
   R3 판정 및 사용자 승인 전 금지(각 단계의 🔒 표기를 따른다).
8. **파괴적 git 작업 금지** — `reset --hard`, 추적 파일 강제 삭제 등(CLAUDE.md 재확인).
   **실행하지 못한 검증을 통과로 기록 금지** — 🔒 미실행이면 Acceptance 미체크.

---

## 6. 리스크 레지스터

| 리스크 | 완화 |
|---|---|
| orchestrate 버전 drift (pin이 실제보다 뒤처짐) | `leaves.manifest.json` 실측 갱신 + doctor.sh 검사 3(drift WARN) |
| claude-codex 조용한 종량 과금 / ToS | R3 전 대량 사용 금지, 호출 자체도 🔒, 판정·수용 기록 |
| basic-memory 클라우드 임베딩 유출 | R4 작업 3 필수 확인(옵션 부재 시 키 unset 확인으로 대체) |
| evidence에 비공개 내용 유입 | R1 작업 4 내용 검토 + [TRUNCATED] 규약 |
| leaf 레포들 빠른 버전 변동(하루 12릴리스 사례) | manifest pinned_commit + doctor.sh drift 검사 |
| grok login MCP dispatch 누락, protocol downgrade | 알려진 결함으로 기록됨. chat 경로는 동작. 수정은 해당 레포 작업(사용자 게이트) |
| 병렬 세션 동시 커밋 | 경계 #6 절차 |
| 크로스-하네스 검증의 실행 주체 혼동 | R3/R4/R5/R6에 headless 커맨드 또는 🔒 명시, 미실행 시 미체크 원칙 |

---

## 7. 결정 기록 (재논의 불필요)

| 날짜 | 결정 | 근거 |
|---|---|---|
| 2026-07-15 | 듀얼-허브 + orca + PAL + 게이트웨이 (원안) | BUILD-SPEC §0.3 |
| 2026-07-16 | 로컬 gemma/qwen 라우터·coordinator 연구 종료 | 독립 split 전 구성 gate `passed=False`, `RESEARCH-CLOSURE.md` |
| 2026-07-16 | orca·PAL·게이트웨이 은퇴 → Codex 콕핏 + orchestrate-codex + leaf 3종 | 실작동 증거(`~/.orchestrate_codex/runs/`), BUILD-SPEC §0.0 |
| 2026-07-16 | "완성" = E1·E2·E3 커밋 증거 3개 | 검증 가능성 |
| 2026-07-16 | 메모리 저장소 = 레포 `memory/data/` | state-in-git 원칙 |
| 2026-07-16 | dual-hub = Codex-primary + Claude Code가 동일 stdio leaf 재사용 | leaf가 하네스 무관 MCP라 추가 코드 0 |
| 2026-07-16 | hubs/claude-code MCP 등록 = 기기 종속 절대경로 + README 주석 | 공백 경로 안전성, 이식성은 주석으로 해결 |
