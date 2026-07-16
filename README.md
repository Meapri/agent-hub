# agent-hub

여러 AI 코딩 하네스(Codex GUI · Claude Code)를 오가며 **일관된 규칙 · 작업 이관 · 메모리**를 공유하고,
**여러 모델을 오케스트레이션**하는 개인용 시스템. 예전엔 substrate 1개 + 외부 플러그인 4개(별도 레포)로
나뉘어 있었으나, 지금은 **하나의 프로젝트(monorepo)로 통합**됐다.

> **정본 원칙**: *"정본은 git, 콕핏·플러그인은 소모품. 상태를 도구 안에 저장하지 않는다."*

## 무엇이 하나로 합쳐졌나

| 구성 | 위치 | 역할 |
|---|---|---|
| **orchestrate_codex** | `src/orchestrate_codex/` | 컨덕터 — 다단계 recipe·advise/step/verify·broker |
| **claude_codex** | `src/claude_codex/` | leaf — Anthropic Claude (구독 OAuth) |
| **grok_codex** | `src/grok_codex/` | leaf — xAI Grok (device-code OAuth) |
| **google_antigravity_codex** | `src/google_antigravity_codex/` | leaf — Google Gemini (OAuth PKCE) |
| **지시 정본** | `instructions/.ruler/` | Ruler → CLAUDE.md·AGENTS.md·.codex·.gemini·.cursor·.mcp.json 생성 |
| **메모리** | `memory/` | basic-memory (FTS 전용, 클라우드 임베딩 차단) |
| **핸드오프** | `handoff/`, `HANDOFF.md` | 하네스 간 작업 이관 규약 |

4개 패키지는 namespace가 안 겹쳐(`orchestrate_codex`/`claude_codex`/`grok_codex`/`google_antigravity_codex`)
**루트 `pyproject.toml` 하나**로 함께 설치된다. 전부 런타임 의존성 0(stdlib), MIT.
각 패키지의 upstream 출처·vendoring 커밋은 [`model-access/leaves.manifest.json`](./model-access/leaves.manifest.json),
저작권 표기는 [`NOTICE.md`](./NOTICE.md)에 있다.

## 설치

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'      # 4 패키지 + console script 한 번에
./.venv/bin/pytest -q                    # 통합 테스트 (204 passed 기준)
./scripts/doctor.sh                      # 건강 점검 5종
```

설치하면 MCP/CLI console script가 venv에 생긴다:
`orchestrate-codex-mcp` · `claude-codex-mcp` · `grok-codex-mcp` · `google-antigravity-mcp`
(+ 각 `*-consent`). 각 leaf는 최초 호출 전 **자체 consent + 구독 OAuth 로그인**이 필요하다.

## 하네스 배선

MCP 서버 5종(memory + orchestrate + 3 leaf)은 `instructions/.ruler/ruler.toml` 한 곳에 정의되고,
`./scripts/sync.sh`가 모든 하네스 설정(`.mcp.json`, `.codex/config.toml`, `.cursor/mcp.json`,
`.gemini/settings.json`)으로 전파한다. 지시는 오직 `instructions/.ruler/`에서만 편집한다(생성물 직접 편집 금지).

```bash
./scripts/sync.sh          # 지시 + MCP 서버를 모든 하네스로 전파
./scripts/check-sync.sh    # 정합 검증
```

> `ruler.toml`과 memory의 env 경로는 **이 기기 기준 절대경로**다(venv 콘솔 스크립트, memory 저장 위치).
> 다른 기기에서 clone하면 경로를 수정하고 `sync.sh`를 다시 돌린다.

## 일상 사용

1. **규칙 변경:** `instructions/.ruler/*.md` → `sync.sh` → `check-sync.sh` → 커밋.
2. **작업:** Codex(주) 또는 Claude Code(보조)에서 진행. 5개 MCP 서버가 로드됨.
3. **메모리:** `memory` MCP로 `memory/data/*.md` 결정·교훈을 읽고 쓴다(하네스 공유).
4. **핸드오프:** `/handoff`로 `HANDOFF.md`에 상태 기록 → 다른 하네스에서 `/takeover`.
5. **다중 모델:** `orchestrate` 컨덕터에 위임하면 host가 적절한 leaf로 라우팅·검증.

### 출력 토큰 정책

Claude·Gemini·Grok 채팅과 Agent Hub의 채팅/작성 단계는 기본 출력 예산을 **65,536토큰**으로 통일한다.
Gemini high-thinking처럼 내부 사고 토큰과 본문이 같은 출력 예산을 쓰는 모델도 긴 문서를 끝까지 작성할 수 있게
여유를 둔 값이다. 호출에서 `max_tokens`를 직접 지정하면 그 값을 우선한다. Gemini 도구 스키마는 최대
131,072토큰까지 허용하며, 모델이 한도에 닿아 `max_tokens`/`length`로 종료하면 부분 출력을 성공으로 처리하지
않고 `incomplete_finish_reason` 오류로 표시한다.

## 저장소 구조

```
agent-hub/
├── pyproject.toml            # 통합 프로젝트 (4 패키지 + console scripts)
├── src/                      # orchestrate_codex · claude_codex · grok_codex · google_antigravity_codex
├── tests/<pkg>/              # 패키지별 테스트 (import-mode=importlib)
├── scripts/                  # *_mcp.py 등 런처 + sync/check-sync/doctor/test-phase1
├── plugins/<slug>/           # 각 플러그인의 skills · manifests · docs (Codex 플러그인 메타)
├── instructions/.ruler/      # 지시 정본
├── memory/  handoff/         # 메모리 · 핸드오프
├── model-access/             # vendored provenance 매니페스트 + 실행 증거
└── BUILD-SPEC.md · EXECUTION-PLAN.md · HANDOFF.md   # 설계·실행·인계 (외부-레포 서술은 통합 이전 기록)
```

## 설계·근거

전체 설계는 [`BUILD-SPEC.md`](./BUILD-SPEC.md), 실행 이력은 [`EXECUTION-PLAN.md`](./EXECUTION-PLAN.md),
현재 인계 상태는 [`HANDOFF.md`](./HANDOFF.md). 이 문서들의 "외부 published 레포 4개" 서술은 통합
**이전** 기록이며, 지금은 위 표대로 `src/`에 vendoring됐다.
