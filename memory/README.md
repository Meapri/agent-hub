# memory — 공유 로컬 메모리 (Phase 3)

두 콕핏(Claude Code · Codex)이 공유하는 로컬 메모리다. **결정·선호·교훈**만 담는다 —
코드 규칙은 지시 정본(`instructions/.ruler/`), 진행 상태는 `HANDOFF.md`가 소유한다.
메모리 서버는 잃어도 되는 보조 리콜 레이어이지 정본이 아니다(BUILD-SPEC §4.4).

- **엔진**: [basic-memory](https://github.com/basicmachines-co/basic-memory) v0.22.1 (평문 Markdown + SQLite).
  MCP 실행: `uvx basic-memory mcp`.
- **노트 저장 위치**: `memory/data/` — **git 추적**. 사람이 읽을 수 있는 Markdown.
- **런타임 상태**: `memory/.basic-memory/` (config.json, memory.db, fastembed 캐시) — **.gitignore**.

## 배선 (환경변수)

`instructions/.ruler/ruler.toml`의 `[mcp_servers.memory]`가 정본이고, `sync.sh`가 각 하네스 설정으로
전파한다. `hubs/*/.mcp.json`에도 동일하게 등록한다. 필수 env 3개:

| 변수 | 값 | 이유 |
|---|---|---|
| `BASIC_MEMORY_CONFIG_DIR` | `.../agent-hub/memory/.basic-memory` | 전역 `~/.basic-memory/config.json`이 이기지 않도록 상태를 격리 |
| `BASIC_MEMORY_HOME` | `.../agent-hub/memory/data` | 노트를 이 repo 안에 저장 |
| `BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED` | `false` | 임베딩 끔 → **네트워크 0, 클라우드 유출 불가**(FTS 전문검색만) |

> **왜 CONFIG_DIR가 필수인가**: 이 기기엔 이미 `~/.basic-memory/config.json`이 있고 거기 등록된
> 프로젝트가 `BASIC_MEMORY_HOME`을 이긴다. 전용 CONFIG_DIR을 주지 않으면 노트가 이 repo로 오지 않는다.

## 임베딩 정책 (Do-Not-Repeat #7)

basic-memory 0.22.1의 기본 임베딩 provider는 로컬 `fastembed`(ONNX)라 노트 내용이 클라우드로 나가지
않는다. `OPENAI_API_KEY`가 있어도 자동 전환되지 않는다(provider를 명시적으로 `openai`/`litellm`로 바꿔야만
호출). 그래도 **가장 확실한 차단**을 위해 semantic search를 아예 꺼서(FTS 전용) 임베딩 모델 다운로드조차
막았다. 리콜 품질이 아쉬우면 대안: `BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED`를 지우고
`BASIC_MEMORY_SEMANTIC_EMBEDDING_PROVIDER=fastembed`로 고정(로컬 임베딩 유지, `FASTEMBED_CACHE_PATH`는
repo 밖으로).

## 이식성

env의 경로는 **이 기기 기준 절대경로**다. 다른 기기에서 clone하면 `ruler.toml`과 `hubs/*/.mcp.json`의
경로를 수정하고 `./scripts/sync.sh`를 다시 돌린다.

## 검증 (doctor)

`./scripts/doctor.sh`가 basic-memory 기동 가능 여부를 점검한다. 크로스-하네스 공유(E3)는
한 하네스에서 노트를 쓰고 다른 하네스에서 읽어 확인한다(EXECUTION-PLAN R4 작업 6).
