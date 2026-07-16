# agent-hub — 멀티모델 오케스트레이션 시스템 빌드 스펙

> **이 문서의 목적**: **개인 업무환경 개선** 프로젝트. 메인 허브 하네스를 콕핏으로 삼고 그 위에
> 플러그인·MCP·자체개발을 얹어 **여러 모델을 일관되게 오케스트레이션**하고, 어떤 하네스로 옮겨가도
> **작업 플로우가 끊기지 않게(핸드오프)** 만든다. 회사 업무·코드·레포와 무관한 순수 개인 프로젝트다.
>
> **허브 결정**: Claude Code와 Codex GUI 앱을 **둘 다** 콕핏으로 세운다(듀얼-허브). 두 앱이 같은
> 5-레이어 확장 모델(지시파일 + Skills + MCP + Subagents + Plugins)로 수렴했기에, **기반(substrate)은
> 한 번만 만들고 두 허브에 배선**하면 된다. 두 허브를 나란히 굴리는 **콕핏으로는 orca(MIT, 로컬)를 포크
> 없이** 얹는다 — worktree 병렬·핸드오프·Gemini/Grok CLI 구동을 이미 제공하므로, 우리는 기반 글루만 만들고
> orca에는 설정(MCP/skills)으로만 배선한다.
>
> 이 문서는 Claude(Fable 5)가 딥 리서치 2트랙(주장별 3-vote 적대적 검증, 기준일 2026-07-15)을 거쳐 작성한
> **실행 스펙**이다. 이어받는 에이전트는 어느 하네스든 이 파일과 `HANDOFF.md`만 읽으면 원 대화 컨텍스트
> 없이 전 과정을 수행할 수 있어야 한다.
>
> **정본(source of truth) 원칙**: 이 시스템의 모든 상태는 **git에 커밋되는 파일**에 산다. 도구는 소모품이다.
>
> **⚠️ 2026-07-16 재기획**: 아래 **§0.0**가 이 문서를 상위 갱신한다. 5-레이어 분해는 유효하지만,
> Phase 4·5의 구현 수단(orca 콕핏 · PAL MCP · LiteLLM/OpenRouter 게이트웨이 · 로컬 gemma/qwen 라우터)은
> 전부 은퇴했다. 이하 본문에서 이 메커니즘들을 서술한 부분과 §0.0가 충돌하면 **§0.0가 우선한다.**

---

## 0.0 재기획 (2026-07-16) — 현실에 맞춘 계획 갱신

> 2트랙 재검토(6개 프로젝트 매핑 + 3각도 독립 재평가 + 적대적 비판, 온디스크 근거 검증 포함)의 결론이다.
> 기존 스펙이 "틀렸다"가 아니라, 현실이 Phase 4·5를 **다른 방식으로 이미 구현**했고 정작 연속성 코어는
> 비어 있어 계획을 현실에 맞춘다.

### 재검토에서 확인된 세 가지
1. **모델접근·오케스트레이션은 이미 다른 방식으로 구현됐고 실제로 작동한다.** `~/.orchestrate_codex/runs/`에
   recipe `deep_readme`가 claude-codex + grok-codex + antigravity 세 leaf를 실제로 협업시켜 완료한 run이
   있다(`status=completed`, verify 통과, 2026-07-16). 단 그 코드·설정이 전부 git 밖에 떠 있다 → **custody 문제.**
2. **연속성 코어(Phase 2 핸드오프 / Phase 3 메모리)는 여전히 미구축**이다. 이게 이 프로젝트의 존재 이유인데
   최근 노력은 거의 전부 모델접근 쪽에 쏠렸다.
3. **로컬 모델 라우팅 연구(gemma/qwen)는 반증됐다.** 누수를 제거한 독립 split에서 어떤 구성도 정적 baseline을
   못 넘겼다(§8-A 참조). 사용자 결정으로 **이 경로는 종료**한다.

### 확정 아키텍처 (현실 반영)
```
        [ Codex GUI = 콕핏/지휘자 ]                         ← 허브 레이어 (Codex-primary)
                 │  orchestrate-codex (conductor MCP: advise/step/verify, broker)
     ┌───────────┼────────────────┬───────────────────┐    ← 모델접근 = provider leaf 플러그인
 claude-codex   grok-codex   antigravity-codex     (+ 향후)
  (Anthropic)     (xAI)       (Google/Gemini)
   ┌─────────────────────────────────────────────────┐     ← 공유 기반 (git, 허브 무관)
   │ 지시    Ruler → CLAUDE.md/AGENTS.md        (Phase 1 ✅)
   │ 핸드오프  HANDOFF.md 규약                   (Phase 2 — 미구축)
   │ 메모리   basic-memory MCP                   (Phase 3 — 미구축)
   └─────────────────────────────────────────────────┘
```
- **콕핏/지휘자**: Codex GUI + orchestrate-codex(호스트 GPT가 지휘). ~~orca~~ 아님.
- **모델접근**: provider별 direct-HTTP MCP **leaf 플러그인**. 각자 구독 OAuth + consent gate. ~~LiteLLM/OpenRouter 게이트웨이~~ 아님, ~~PAL~~ 아님.
- **워커 스폰**: 필요 시 orchestrate-codex `broker.py`가 leaf MCP를 subprocess로 스폰. ~~PAL MCP~~ 아님.
- **위치**: 네 플러그인은 별도 published 레포(`github.com/Meapri/{claude-codex,grok-codex,google-antigravity-codex,orchestrate-codex}`).
  agent-hub는 이들을 **매니페스트(레포 URL + pinned commit)로 등록**해 정본에 편입한다.

### "완성"의 새 정의 — 커밋된 승인 증거 3개
5-Phase 체크박스 대신 **검증 가능한 아티팩트**로 정의한다.
1. **모델접근·오케스트레이션**: agent-hub에 커밋된 leaf 매니페스트 + 실제 orchestrate run 트랜스크립트 1개.
2. **핸드오프**: Claude Code ↔ Codex 실제 작업 왕복 1회를 HANDOFF.md에 기록.
3. **메모리**: 한 하네스에서 쓴 메모리를 다른 하네스에서 읽은 것 1회.

### 새 빌드 순서 (우선순위)

> **실행 절차·수용 기준·경계의 확정판은 [`EXECUTION-PLAN.md`](./EXECUTION-PLAN.md)다** (단계 R0~R6).
> 아래 표는 방향 요약이며, 실행 시 EXECUTION-PLAN이 우선한다.

| EXECUTION-PLAN 단계 | 작업 | 이유 | 상태 |
|---|---|---|---|
| R0 🔒 | orchestrate-codex v0.5.1 push, R3 콘솔 확인, leaf OAuth (사용자 행동) | 작동 conductor가 unpushed — 현 최대 리스크 | 대기 |
| R1 | **custody**: agent-hub에 leaf 매니페스트 + run 증거 커밋 | "작동하지만 git 밖" 해소 (E1) | 다음 |
| (R2) | 문서 동기화 (§0.0 + §5/§6/§8 + HANDOFF/README) | 인계 에이전트가 실재 시스템을 재건하도록 | ✅ 2026-07-16 |
| (완료) | 로컬 라우터·coordinator 정리 (아카이브) | 반증·중복 제거 | ✅ `90510db` |
| R3 🔒 | **claude-codex 과금/ToS 확인** | live 호출 이미 발생, 구독 vs 종량 미검증 | 대기 |
| R4 | **Phase 3 메모리 배선** (basic-memory) → E3 | 가장 싼 진짜 공백 | 대기 |
| R5 | **Phase 2 크로스-허브 핸드오프 실연** → E2 | killer demo, 프로젝트 목적 | 대기 |
| R6 | **hubs/ 재배선 + dual-hub 확정** (Codex-primary + Claude Code leaf 재사용) | 출하물이 전부 Codex | 대기 |

> 이하 §1~§9는 원래 스펙(2026-07-15)이다. §0.0와 어긋나는 메커니즘 서술에는 `[SUPERSEDED §0.0]` 마커를 달았다.

---

## 0. 이어받는 에이전트에게 — 먼저 읽어라

### 0.1 이 문서를 쓰는 방식
- 이 스펙은 **명령이 아니라 설계 근거 + 실행 순서**다. 각 단계에는 **완료 판정 기준(Acceptance)**이 붙어 있다.
- 단계마다 작은 git 커밋을 남기고, 다음 단계로 넘어가기 전 Acceptance를 통과시켜라.
- **한 번에 한 에이전트만 코드를 쓴다.** 네가 작업하는 동안 다른 에이전트를 같은 파일에 붙이지 마라.
- 작업 시작 시 `HANDOFF.md`를 먼저 읽고, 종료 시 갱신해 커밋하라.

### 0.2 반드시 스스로 검증할 것 (이 스펙의 한계)
이 문서의 버전 번호·"지원한다"류 주장은 **2026-07-15 스냅샷**이다. 이 카테고리는 교체 주기가 빠르다.
따라서 **설치·배선 직전에 직접 확인**하라:
- 각 도구의 최신 릴리스와 설치 명령 (`npm view`, `pipx`, GitHub releases)
- 각 하네스의 MCP·플러그인 설정 파일 경로/스키마가 바뀌지 않았는지 (아래 표들은 스냅샷)
- Ruler / basic-memory / 게이트웨이 / 워커-CLI MCP 가 아직 유지되는지 (커밋 최근성)
검증에서 반증된 주장은 §8 "Do-Not-Repeat"에 명시했다. 그대로 밟지 마라.

### 0.3 시스템 모델 = 공유 기반 + 2개 허브 콕핏

핵심 통찰: "플로우가 안 이어진다 + 모델마다 성향이 달라 일관성이 없다"는 한 문제 같지만, **기반(허브 무관)**과
**허브(콕핏별)**로 쪼개진다. 기반은 파일·MCP라서 어느 허브에서나 그대로 읽히고, 허브는 그 기반을 배선하는
얇은 껍질이다.

```
        [ Claude Code 콕핏 ]        [ Codex 콕핏 ]        ← 허브 레이어 (Phase 5, 콕핏별)
                 └──────────┬──────────────┘
                            │  (동일 기반을 두 허브에 배선)
   ┌────────────────────────┴─────────────────────────┐   ← 공유 기반 (허브 무관, 한 번만 구축)
   │ 지시 일관성    Ruler → CLAUDE.md + AGENTS.md 동시   (Phase 1 ✅)
   │ 연속성         HANDOFF.md 규약 (Phase 2) + basic-memory MCP (Phase 3)
   │ 모델 접근      게이트웨이 + 워커-CLI MCP = "여러 모델" 엔진 (Phase 4)
   └──────────────────────────────────────────────────┘
```

| 레이어 | 문제 | 해법 | 허브 의존성 | Phase |
|---|---|---|---|---|
| 지시 일관성 | 하네스마다 규칙이 다르게 전달됨 | 단일 정본 → Ruler로 CLAUDE.md·AGENTS.md 동시 생성 | 무관(둘 다 생성) | 1 ✅ |
| 연속성-핸드오프 | 진행 중 작업을 못 이어받음(허브 간 포함) | HANDOFF.md 규약 + git 경계 | 무관(파일) | 2 |
| 연속성-메모리 | A가 배운 걸 B가 모름 | 로컬 stdio MCP 메모리 서버 1개 | 무관(MCP) | 3 |
| 모델 접근 | 여러 모델을 한 콕핏에서 못 굴림 | 게이트웨이(API 모델) + 워커-CLI MCP(다른 하네스) | 무관(MCP/gateway) | 4 |
| 허브 패키징 | 기반을 콕핏에 배선·오케스트레이션 UX | Claude Code 플러그인 + Codex 플러그인 | **허브별** | 5 |

**핵심 발견 두 가지**:
1. 이 문제를 통째로 푸는 단일 제품은 2026-07 현재 **없다**. "브랜드가 다른 하네스 간 대화 상태의 완전한
   이관"은 구조적으로 불가(툴 스키마 비호환 → 툴콜 이력이 프로즈로 뭉개짐). 그래서 이관 단위를 **"대화"가
   아니라 "저장소 아티팩트"로** 바꾼다.
2. **"여러 모델 오케스트레이션"의 뼈대(허브가 타모델 에이전트를 스폰)는 이미 오프더셸프**로 있다(PAL MCP 등,
   §6). 자체개발은 그걸 재발명하지 말고 **개인 메모리·핸드오프·일관성 글루**에 쓴다.

---

## 1. 저장소 구조 (목표)

이 저장소(`agent-hub/`)가 시스템 전체의 정본이다. 도구가 다 죽어도 이 레포만 있으면 재구축된다.
`✅`는 Phase 1에서 이미 구현·커밋됨.

```
agent-hub/
├── BUILD-SPEC.md            # 이 문서 (설계 근거 + 빌드 스펙)
├── HANDOFF.md               # 살아있는 핸드오프 기록 (현재 상태·다음 할 일)
├── README.md                # 시스템 한 장 요약 ✅
├── instructions/            # 지시 일관성 (기반)
│   └── .ruler/              ✅  00-persona / 10-coding / 20-workflow / 90-forbidden / ruler.toml
├── CLAUDE.md  AGENTS.md  .gemini/settings.json  .codex/config.toml   # Ruler 생성물(직접 편집 금지) ✅
├── scripts/                 ✅  sync.sh / check-sync.sh / test-phase1.sh  (+ Phase 3: doctor.sh)
├── handoff/                 # 연속성-핸드오프 (Phase 2)
│   ├── HANDOFF.template.md
│   └── commands/            #   claude/ · codex/ · gemini/ 의 /handoff·/takeover
├── memory/                  # 연속성-메모리 (Phase 3)
│   ├── README.md            #   basic-memory 기동·등록법
│   └── data/                #   Markdown 저장소 (git 추적)
├── model-access/            # 모델 접근 엔진 (Phase 4)
│   ├── gateway/             #   LiteLLM/OpenRouter 설정 (API 모델 라우팅)
│   └── workers/             #   워커-CLI MCP(PAL 등) 등록·래퍼
└── hubs/                    # 허브 패키징 (Phase 5, 콕핏별)
    ├── claude-code/         #   플러그인: skills/subagents/commands/hooks/mcp
    └── codex/               #   플러그인: skills/mcp/app-connectors/hooks
```

---

## 2. Phase 1 — 지시 일관성 ✅ (완료)

### 2.1 배경 (왜 이렇게 하는가)
- **AGENTS.md**가 사실상 표준(약 23개 하네스 네이티브: Codex, Gemini CLI, Cursor, Copilot, Grok Build 등).
  **유일한 대형 홀드아웃은 Claude Code** — `CLAUDE.md`만 읽는다. → 그래서 정본 1개에서 **양쪽을 생성**한다.
- **함정**: 지시 파일을 에이전트에게 자동 생성시키지 마라. 기계 생성 AGENTS.md는 수기 큐레이션 대비 ~3% 성능
  저하 보고(Addy Osmani, 2026-03). **정본은 손으로 다듬고, 배포만 자동화한다.**

### 2.2 구현된 상태 (커밋 e885454 → e76d5a6)
- 도구: **Ruler** `0.3.44` 고정(`intellectronica/ruler`). rulesync 아님 — rulesync의 MCP 전파 주장은 검증서 반증(§8).
- 정본: `instructions/.ruler/00-persona.md · 10-coding.md · 20-workflow.md · 90-forbidden.md` (유일 편집 지점).
- 설정: `instructions/.ruler/ruler.toml` — 타깃 `claude`(→CLAUDE.md), `codex`(→AGENTS.md + `.codex/config.toml`),
  `gemini-cli`(→AGENTS.md), `cursor`(→AGENTS.md). `[mcp] merge_strategy="merge"`, `nested=false`, skills off(Phase 1 한정).
- 생성물: `CLAUDE.md`, `AGENTS.md`, `.gemini/settings.json` — git 추적, 직접 편집 금지.
- 스크립트: `sync.sh`(임시 `.ruler` bridge로 apply), `check-sync.sh`(추적·정합·bridge cleanup 확인),
  `test-phase1.sh`(`/tmp` disposable fixture 회귀).

### 2.3 Acceptance (통과 완료)
- [x] `sync.sh` 실행 시 CLAUDE.md와 AGENTS.md가 **동일 정본에서 생성**(수동 복사본 아님).
- [x] `.ruler/` 한 곳만 고치고 sync 하면 모든 타깃 갱신.
- [x] disposable fixture에서 수동 드리프트 → sync 복구, 정본 변경 양쪽 전파, 재실행 idempotence 확인.
- [x] 다른 저장소에는 파일·브랜치·ref 변경 없음(검증 범위에서 제외).

### 2.4 남은 리스크 (HANDOFF.md 참조)
Ruler 0.3.44에 외부 source-dir 옵션이 없어 임시 symlink bridge 사용 — 공식 CLI 계약 아니므로 버전 갱신 전 재검증,
`--no-nested` 유지. apply는 다중 타깃 트랜잭션이 아니라 중간 실패 시 부분 갱신 가능 → 성공 후 check + git diff.

---

## 3. Phase 2 — 연속성·핸드오프 (다음, 듀얼-허브 핵심)

> 이 Phase가 "둘 다"의 킬러 데모다: **Claude Code ↔ Codex 간 실제 작업 이관**을 성립시킨다.

### 3.1 원칙
브랜드 간 대화 상태 이관은 포기하고, **이관 단위를 저장소 아티팩트로 바꾼다.** 커뮤니티 합의: 화려한 세션 변환
도구보다 **구조화된 HANDOFF.md + 작은 git 커밋**이 실전에서 더 잘 작동한다. 규칙: 단계마다 작은 커밋,
`git diff`를 핸드오프에 첨부, **한 번에 한 에이전트만 쓰기**, 이어받는 쪽은 **항상 HANDOFF.md부터**.

### 3.2 3티어 (기본 먼저, 나머지 보조)
- **1티어 (기본)**: `HANDOFF.md` 패킷 + git 경계. 아래 템플릿을 `handoff/HANDOFF.template.md`로 저장.
- **2티어 (비상용)**: 세션 변환 — `cli-continues`, authsec-bridge, **Codex CLI v0.128+의 Claude 세션 native import(`/import`)**.
  급할 때 컨텍스트 주입용으로만. 의존 금지(포맷 비공식·버전 취약).
- **3티어 (긴 작업)**: spec-driven — GitHub Spec Kit / OpenSpec. 의도(spec/plan/tasks)를 git 파일로 유지.

### 3.3 HANDOFF.md 패킷 템플릿 (handoff/HANDOFF.template.md)
```markdown
# HANDOFF — <작업명>

> 이건 요약이 아니다. **다음 에이전트를 위한 복구 기록**이다.

- **원래 목표**: <이 작업이 궁극적으로 달성하려는 것>
- **현재 단계**: <전체 계획 중 지금 어디까지> — <어느 허브에서 작업 중이었는지>
- **완료**: <끝난 것 — 파일/커밋 해시와 함께>
- **미완**: <남은 것 — 구체적으로>
- **변경 파일**: <git diff 요약. `git diff > handoff-<name>.diff` 첨부 권장>
- **검증 실행 결과**: <어떤 테스트/명령을 돌렸고 결과가 무엇이었는지 — 실제 출력>
- **현재 리스크**: <알려진 위험·의심스러운 부분>
- **Do-Not-Repeat**: <이미 시도했다 실패한 것>
- **다음 한 걸음**: <이어받는 에이전트가 즉시 할 단 하나의 행동> — <권장 허브가 있으면 명시>
```

### 3.4 커맨드 심기 (handoff/commands/) — 양쪽 허브
`/handoff`(현 상태를 템플릿으로 출력)와 `/takeover`(HANDOFF.md 읽고 상태 복원)를 두 허브 모두에 정의:
- **Claude Code**: 스킬 또는 `.claude/commands/handoff.md` 슬래시 커맨드. (Phase 5에서 플러그인으로 번들)
- **Codex**: `.codex/` 프롬프트 / 플러그인 스킬.
- (선택) **Gemini CLI**: `.gemini/commands/`.
Ruler로 배포 가능한 공통 부분은 정본화하고, 하네스별 포맷 차이만 `handoff/commands/<harness>/`에 둔다.

### 3.5 Acceptance (Phase 2 완료 판정)
- [ ] `HANDOFF.template.md` 존재.
- [ ] **크로스-허브 리허설**: 실제 작업 하나를 **Claude Code에서 시작 → HANDOFF.md 작성 → Codex에서 이어받아 완료**,
      그리고 역방향(Codex → Claude Code)도 성립. 이어받은 쪽은 **원 대화 없이** HANDOFF.md만으로 "다음 한 걸음" 수행.
- [ ] `/handoff`·`/takeover`가 **양쪽 허브(Claude Code, Codex)에서** 동작.

### 3.6 보고된 실패 모드 (피할 것)
패킷 과대 · 스테일 컨텍스트로 재개 · 두 에이전트가 같은 파일 동시 편집 · 세션 파서가 버전업에 파손 ·
리뷰어가 수정까지 해버림(리뷰·수정 분리) · 핸드오프를 전자동 파이프라인으로 착각.

---

## 4. Phase 3 — 연속성·메모리 (공유 MCP)

### 4.1 왜 MCP인가
MCP는 2026-07 현재 **모든 주요 코딩 하네스가 채택한 유일한 공통 프로토콜**. 로컬 CLI는 전부 **stdio**를
지원하므로, **로컬 메모리 서버 1개를 두 허브에 등록**하면 공유 메모리가 되고 **코드/메모가 기기를 안 떠난다**.

### 4.2 메모리 서버 선택: basic-memory (시작점)
| 후보 | 특징 | 적합성 |
|---|---|---|
| **basic-memory** ✅ | 로컬 Markdown + SQLite 위키링크 그래프 | **1순위** — 평문이라 도구가 죽어도 데이터가 git에 남음 |
| mcp-memory-service | SQLite-vec + MCP·REST, 자동 통합, 에이전트별 태깅 | 리콜 품질 아쉬우면 이전 |
| Graphiti / cognee | 시간적 KG / KG+벡터 | 개인용으론 과체중 |

저장 디렉토리는 `agent-hub/memory/data/`로 잡아 git 추적.

### 4.3 등록 (양쪽 허브) — 스냅샷, 경로 재확인
| 하네스 | MCP 설정 위치 | 로컬 stdio |
|---|---|---|
| Claude Code | `.mcp.json` / `~/.claude.json` | ✅ |
| Codex CLI | `~/.codex/config.toml` `[mcp_servers]` | ✅ |
| Gemini CLI | `~/.gemini/settings.json` `mcpServers` | ✅ |
| Cursor | `.cursor/mcp.json` | ✅ |
| ChatGPT/Grok **웹** | Developer Mode / connectors | ❌ 원격만(공개 HTTPS 터널 필요) |

→ 로컬 하네스는 Ruler `ruler.toml`의 `[mcp_servers.memory]` 정의 한 번으로 두 허브에 동시 등록(주석으로 이미 예약됨).
ChatGPT/Grok 웹까지 원하면 Tailscale Funnel로 HTTPS 노출(선택).

### 4.4 함정 (반드시 방어)
1. **임베딩 유출**: 환경에 `OPENAI_API_KEY`/`GEMINI_API_KEY`가 있으면 자동 감지로 클라우드 임베딩에 보내는 도구가 있다.
   **로컬 임베딩(Ollama/all-MiniLM) 또는 풀텍스트를 명시적으로 고정.**
2. **역할 분리**: 메모리엔 **"결정·선호·교훈"만**. 코드 규칙 → 지시 정본(Phase 1), 진행 상태 → HANDOFF.md(Phase 2).
   메모리 서버는 잃어도 되는 보조 리콜 레이어. 정본으로 삼지 마라.

### 4.5 Acceptance
- [ ] basic-memory 로컬 stdio 기동, 클라우드 호출 없음(임베딩 로컬 고정 확인).
- [ ] Claude Code에서 저장한 메모리를 Codex에서 읽음(같은 저장소 공유).
- [ ] `memory/data/`가 git에 커밋, 사람이 읽을 수 있는 Markdown.
- [ ] `scripts/doctor.sh`로 두 허브의 MCP 등록 상태 점검.

---

## 5. Phase 4 — 모델 접근 레이어 (= "여러 모델" 엔진)

> **[SUPERSEDED §0.0]** 이 Phase는 실현 방식이 바뀌어 **충족**됐다. §5.1 게이트웨이·§5.2 PAL 대신,
> provider별 direct-HTTP MCP **leaf 플러그인**(claude-codex·grok-codex·antigravity-codex)을
> **orchestrate-codex**가 지휘하는 구조로 이미 작동한다. 남은 일은 새 플러밍 구축이 아니라 §0.0의 custody
> (leaf 매니페스트·run 증거 커밋)다. 아래 §5.1~§5.4는 은퇴한 원안이니 참고만.

> 이게 "많은 모델 오케스트레이션"의 실체다. 두 채널 모두 **MCP/게이트웨이**라서 두 허브가 공유한다.

### 5.1 채널 A — API 모델을 게이트웨이로
**LiteLLM(셀프호스팅, `localhost:4000`)** 또는 **OpenRouter**로 GPT·Gemini·Grok·Claude·로컬(Ollama)을 OpenAI 호환
1엔드포인트에 모은다. 키·예산·폴백·비용추적 단일화. 콕핏의 subagent가 모델을 바꿔 부를 때 이 엔드포인트를 가리킨다.
- Claude Code: subagent 정의에 **모델 오버라이드 네이티브**(Opus 리드 / 저가 워커 티어링).
- Codex: 게이트웨이 모델을 MCP/설정으로 노출.

### 5.2 채널 B — 다른 CLI를 워커로 스폰
그 하네스의 *행동*까지 필요할 때(모델뿐 아니라 툴·성향), 허브가 다른 CLI를 서브프로세스 워커로 돌린다.
**오프더셸프 우선 — 재발명 금지**:
- **PAL MCP**(BeehiveInnovations) — Claude/Gemini/Codex + OpenRouter/Grok/Ollama를 하나로, 상호 subagent 스폰.
- **multi_mcp** — CLI 모델을 서브프로세스로.
- **maestro-orchestrate** — Gemini/Claude/Codex/Qwen 다중 스페셜리스트.
`model-access/workers/`엔 선택한 MCP의 등록·래퍼만 둔다.

### 5.3 Acceptance
- [ ] 두 허브 각각에서 **비-허브 모델 1개**를 게이트웨이 경유로 호출 성공.
- [ ] 두 허브 각각에서 **다른 CLI 1개를 워커로 스폰**해 결과 회수 성공.
- [ ] 회사/민감 코드 경로는 게이트웨이에서 **로컬 모델로 라우팅**되도록 구성(코드가 기기를 안 떠남).

### 5.4 관제(선택, 얇게)
콕핏 자체가 병렬 관제를 상당 부분 흡수한다(Claude Code Agent View/Agent Teams, Codex multi-agent v2).
별도 매니저(emdash 등)가 필요하면 **상태를 도구에 저장하지 않는 조건**으로만 겉에 걸친다(churn 극심). A2A는 제외(§8).

---

## 6. Phase 5 — 허브 패키징 (콕핏별, 유일한 허브-의존 부분)

> **[SUPERSEDED §0.0]** 실질 패키징은 **agent-hub 밖 published 레포 4종**으로 이미 일어났다
> (claude-codex·grok-codex·antigravity-codex leaf + orchestrate-codex conductor). **콕핏은 orca가 아니라
> Codex GUI**이고, 지휘자는 orchestrate-codex(호스트 GPT)다. in-repo `hubs/claude-code`·`hubs/codex`
> 스캐폴드와 그 안의 `pal` MCP 엔트리는 한 번도 실행된 적 없는 **낡은 껍데기**이므로, §0.0의 매니페스트
> 방식으로 대체하거나 실제 배선에 맞게 고쳐야 한다. 아래 원안은 참고만.

기반(Phase 1~4)을 두 콕핏에 **플러그인 1개씩으로 배선**한다. 이게 "install once → 오케스트레이션 셋업 전부" 단위.
콕핏 셸은 **orca(미포크)**가 맡고, orca가 Claude Code·Codex를 띄울 때 각 플러그인이 활성화된다.

- **Codex 플러그인** (`hubs/codex/`) ✅ **스캐폴드 완료**: `.codex-plugin/plugin.json` + `.mcp.json`(memory=basic-memory,
  pal=PAL MCP) + skills(`handoff`/`takeover`/`route-to`) + README. 설치·실기검증은 미완(README의 "설치 직전 확인" 참조).
  Codex multi-agent v2에서 서브에이전트가 부모 플러그인/MCP를 상속.
- **Claude Code 플러그인** (`hubs/claude-code/`) ✅ **스캐폴드 + `claude plugin validate` 통과**: `.claude-plugin/plugin.json`
  + `.mcp.json`(mcpServers: memory=basic-memory, pal=PAL MCP) + skills(`handoff`/`takeover`/`route-to`) + README.
  설치·MCP 실기동은 미완(도구·키 필요). 향후 확장: 모델별 커스텀 subagent 타입, hooks, Workflow 결정적 팬아웃.

### 6.1 Acceptance
- [ ] 새 clone에서 **각 허브 플러그인 설치 → 기반(지시·메모리·모델접근)이 자동 배선**됨.
- [ ] 같은 오케스트레이션 작업을 두 콕핏에서 각각 수행 가능(A/B 비교).
- [ ] "어느 콕핏이 어떤 작업에 맞나" 짧은 스코어카드 기록(오케스트레이션 편의·모델 라우팅·플러그인 개발 마찰·GUI).

---

## 7. 전체 빌드 순서

> **[SUPERSEDED §0.0]** 현행 우선순위는 §0.0 "새 빌드 순서" 표를 따른다(custody → 문서 → 정리 → 과금확인
> → 메모리 → 핸드오프 → 범위결정). 아래 표는 Phase↔레이어 대응만 참고.

| Phase | 레이어 | 산출물 | 상태 |
|---|---|---|---|
| 1 | 지시 일관성 | `.ruler/` 정본 + 생성물 + 스크립트 | ✅ 완료 |
| 2 | 연속성-핸드오프 | HANDOFF.template + 양쪽 커맨드 + 크로스-허브 리허설 | 다음 |
| 3 | 연속성-메모리 | basic-memory MCP + doctor.sh | |
| 4 | 모델 접근 | 게이트웨이 + 워커-CLI MCP | |
| 5 | 허브 패키징 | Claude Code 플러그인 + Codex 플러그인 | |

각 Phase 종료 시 `HANDOFF.md`를 갱신하고 커밋한다.

---

## 8. Do-Not-Repeat (검증에서 반증되었거나 함정으로 확인된 것)

### 8-A. 2026-07-16 재기획 추가 교훈 (아래 원목록보다 우선)
- **A1. 로컬 모델로 provider 라우팅을 학습시키려 하지 마라 — 반증됨.** gemma-4-e4b/qwen QLoRA 라우터는
  v1~v4 네 사이클을 돌고도 누수 제거 독립 split에서 정적 baseline(`gpt-5.6-sol-high`, primary 55.2% / regret 1.94)을
  **한 번도** 못 넘었다(최선 capped240 primary 51.9% / regret 3.63; 전 구성 promotion gate `passed=False`).
  라벨이 GPT-win에 60~70% 쏠려 구조적으로 어렵다. **연구 종료, 계획에서 제외.**
- **A2. orchestrate-codex가 이미 하는 오케스트레이션을 in-repo로 재발명하지 마라.** uncommitted
  `coordinator.py`/`run_qwen_orchestration.py`(prompt-only 멀티스텝)는 orchestrate-codex broker와 중복이다.
- **A3. #6/#11 정정.** 실제로 PAL·orca를 **안 쓰기로** 판명났다. 크로스-CLI 스폰은 orchestrate-codex broker가
  자체 구현했고(작동), 콕핏은 Codex GUI다. 아래 #6(PAL 사용)과 #11(orca)은 **§0.0로 대체**됐다.
- **A4. 작동하는 통합을 git 밖에 방치하지 마라.** 유일하게 증명된 통합이 uncommitted 코드 + off-git
  `~/.orchestrate_codex/leaves.json` + 외부 레포에 얹혀 있다. 정본 원칙 위반이자 최대 리스크(§0.0 step 0).

1. **rulesync로 MCP를 전파하려 하지 마라.** "MCP/서브에이전트/스킬 동기화" 주장은 3-vote 검증서 반증(0/3). MCP는 **Ruler**로.
2. **지시 파일을 에이전트에게 자동 생성시키지 마라.** 기계 생성 ~3% 성능 저하. 정본은 수기, **배포만** 자동화.
3. **CLAUDE.md/AGENTS.md/생성물을 직접 편집·수동 복사하지 마라.** 반드시 드리프트. `instructions/.ruler/`만 고치고 sync.
4. **메모리 서버를 정본으로 삼지 마라.** 카테고리가 젊다. 잃어도 되는 데이터만.
5. **세션 변환 도구에 의존하지 마라.** 툴콜은 프로즈로 뭉개지고 포맷 취약. 비상 컨텍스트 주입용으로만.
6. **크로스-CLI 스폰을 자체개발로 재발명하지 마라.** PAL MCP 등 오프더셸프 사용. 자체개발은 메모리·핸드오프·일관성 글루에.
7. **클라우드 임베딩 키가 새지 않게 하라.** 로컬 임베딩 고정. 민감 코드는 게이트웨이에서 로컬 모델로.
8. **A2A를 넣지 마라.** 이 문제에 유용하다는 근거 0.
9. **사용자 요청 없이 다른 저장소에 이 시스템을 적용·수정하지 마라.** (개인 프로젝트 경계)
10. **매니저/콕핏 도구 안에 작업 상태를 저장하지 마라.** 정본은 항상 git.
11. **orca(및 콕핏 도구)를 포크·패치하지 마라.** 일일 릴리스 타깃이라 rebase 지옥 + 소모품 원칙 위반. config/MCP/skill로 해결하거나 upstream PR. 커스텀은 git 기반(substrate)에.

---

## 9. 출처 (3-vote 검증 통과 핵심)

- AGENTS.md 표준: https://agents.md/ · Ruler: https://github.com/intellectronica/ruler
- MCP 크로스-벤더: https://developers.openai.com/codex/mcp
- 허브 확장/오케스트레이션: https://code.claude.com/docs/en/agent-teams · https://thenewstack.io/openais-codex-gets-plugins/
- 멀티모델 엔진(오프더셸프): https://github.com/BeehiveInnovations/pal-mcp-server · https://github.com/religa/multi_mcp · https://github.com/josstei/maestro-orchestrate
- 모델 게이트웨이: LiteLLM(셀프호스팅) · OpenRouter
- 메모리: https://github.com/basicmachines-co/basic-memory · https://github.com/doobidoo/mcp-memory-service
- 핸드오프: https://knightli.com/en/2026/07/10/codex-claude-code-task-handoff-guide/ · https://github.com/yigitkonur/cli-continues
- spec-driven: https://github.com/github/spec-kit · https://github.com/Fission-AI/OpenSpec
- 개인 MCP 자작 사례: https://rundatarun.io/p/i-built-a-personal-memory-system

원 리서치 보고서(시각화): https://claude.ai/code/artifact/aff95c0a-2854-4261-ad07-9e90e96c28f5
