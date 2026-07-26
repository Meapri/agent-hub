# HANDOFF — Agent Hub

> 이건 요약이 아니다. **다음 에이전트(어느 하네스든)를 위한 복구 기록**이다.
> 현재 구조와 사용법은 [`README.md`](./README.md), 실행 계약은
> [`docs/architecture/agent-hub-v2-protocol.md`](./docs/architecture/agent-hub-v2-protocol.md)를 기준으로 합니다.
> 현재 작업 상태와 다음 한 걸음은 아래 `agent-hub:handoff:v1` managed block이 최신입니다.

- **원래 목표**
  여러 AI 코딩 에이전트(Claude Code · Codex/ChatGPT · Antigravity CLI · Grok · Cursor)를 한 사람이 쓸 때
  **작업 플로우가 끊기지 않고(핸드오프) 모델 성향과 무관하게 일관적으로 작동하는** 개인용 시스템을 이 레포에 구축한다.
  정본 원칙: 모든 상태는 Git에 커밋되는 파일에 산다. 도구는 소모품이다.

<!-- agent-hub:handoff:v1:start -->
- **원래 목표**: Agent Hub v2 광범위 발전 계획을 구현하고, 실제 설치·네 provider 검증·문서·커밋·push까지 완료합니다.
- **현재 단계**: v2.1.0 구현과 로컬 설치, provider E2E canary, 전수 검증을 완료했습니다. HANDOFF 반영 후 같은 변경을 commit/push하는 단계입니다.
- **완료**:
  - deep inspect fact pack, stable digest, artifact egress approval, provider destination fencing, strict task/plan/provider contract를 구현했습니다.
  - SQLite schema 7에 content-free operation metrics, lease renewal, idempotency request digest, 안전한 recovery/event, artifact provenance tombstone을 구현했습니다.
  - declared fallback·pinned routing·DAG critical path·deterministic verifier·부분 재계획을 보강했습니다.
  - provider worker를 request별 proxy port, 환경변수 allowlist, 임시 cwd, provider state 쓰기 범위, process group cancel, correlation ID, untrusted context 경계로 강화했습니다.
  - versioned `stage-release`, executable/source/Python digest, candidate DB-copy health, rollback DB snapshot, setup transaction/rollback과 runtime-aware doctor를 구현했습니다.
  - staging 후 console-script shebang이 임시 경로를 가리키는 결함과 LaunchAgent에 HOME이 없어 GPT generation만 실패하는 결함을 실제 설치 과정에서 재현·수정했습니다.
  - runtime `/Users/naen/.agent-hub/releases/2.1.0-0bc8636e9611`을 설치하고 LaunchAgent·Codex/Claude/Cursor/Gemini MCP config를 같은 bridge로 연결했습니다. Codex와 Claude plugin도 2.1.0으로 갱신했습니다.
  - Claude `claude-opus-5`, Grok `grok-4.5`, Gemini `gemini-3.6-flash-high`, GPT `gpt-5.6-sol`이 각각 실제 generation으로 `AGENT_HUB_V21_OK`를 반환했습니다.
  - 감사 run `0541277e02d8eb73`의 최종 보고 artifact는 `art_b2a01b1fad2ac86a71a8cdc9`, digest `102a51c1ce4dcb98034e96b6be56d3f7181fdf1b733e816ca89955b6ccd963ef`이며 authenticated입니다.
- **미완**: `release.py::apply_switch`에서 daemon stop 이후 DB restore 자체가 예외를 내는 창의 완전한 보상 transaction, dependency lock/hash 기반 offline staging, incremental context index, bounded result-class metrics는 다음 발전 항목입니다.
- **변경 파일**: v2 kernel(`context.py`, `contracts.py`, `service.py`, `store.py`, `routing.py`), provider runtime, release/setup/stage/doctor, README/protocol, plugin/version manifest, 관련 테스트 20여 개를 변경했습니다.
- **검증 실행 결과**: 전체 pytest `550 passed, 2 skipped`; 수정 파일 Ruff format/check; README `verify_text(user_facing=true)`와 document quality; Ruler sync; hub plugin check; release version sync; `git diff --check`; sdist/wheel build를 통과했습니다. live doctor는 `7 pass, 0 warn, 0 fail`, DB schema 7/WAL/integrity OK, LaunchAgent running입니다.
- **현재 리스크**: update/rollback의 DB restore 함수 자체가 daemon stop 뒤 실패하면 자동 보상 경로가 아직 완전하지 않습니다. pip dependency resolution이 lock/hash로 고정되지 않아 동일 source digest가 제3자 dependency까지 완전히 재현하지는 않습니다. 기존 설치 과정에서 생성된 깨진 runtime은 `/Users/naen/.Trash/agent-hub-2.1.0-4625777943f5-broken-stage`로 이동해 복구 가능하게 보존했습니다.
- **Do-Not-Repeat**: connected/ready를 generation 성공으로 표현하지 마세요. versioned venv를 임시 경로에서 rename할 때 shebang relocation 후 최종 경로 실행 검증을 생략하지 마세요. LaunchAgent provider subprocess에는 HOME을 명시하세요. active external step을 중복 retry하지 마세요. 감사 LLM artifact의 snapshot 지적을 현재 코드 재현 없이 확정 결함으로 옮기지 마세요.
- **다음 한 걸음**: `src/agent_hub/v2/release.py::apply_switch`의 bootout→DB restore→bootstrap 전체를 보상 transaction으로 감싸고, `tests/agent_hub/test_v2_release.py`에 restore 예외 후 plist·DB·기존 daemon 복구 테스트를 추가하세요.
<!-- agent-hub:handoff:v1:end -->
