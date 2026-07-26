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
- **원래 목표**: Agent Hub V2-only의 clean macOS 설치를 안정화하고, Agent Hub 자체로 전체 구조를 감사해 다음 발전 우선순위를 증거 기반으로 확정합니다.
- **현재 단계**: bootstrap 수정은 commit `326ff0a3fe2f8432a37c7e2da9e67420896ccfc9`로 `origin/main`에 push했습니다. 감사 run `dcb9656ae0bf2c01`은 revision 22, `completed`, routing mode `shadow`입니다.
- **완료**:
  - Python 3.10+ 자동 선택, `AGENT_HUB_PYTHON` override, 비호환 기존 `.venv` 안전 중단을 구현했습니다.
  - clean install, 공개 도구 14개, 네 provider manifest protocol 2.0, SQLite schema 4/WAL/integrity, daemon restart 뒤 queued run 보존을 검증했습니다.
  - 37개 파일 egress를 승인해 7-step 감사 DAG를 실행했습니다. plan digest는 `7bfc0825bad6a8e4508b2c77c983f3138e3d5d8b9cee1794c1bb0573905aac74`, policy revision은 2입니다.
  - 네 local inspect, Claude 초안, GPT 검토, Claude 최종 통합을 완료했습니다. final artifact `art_370412b896dd2ad2f447e50e`, digest `dce3d1eda36b58ab7272afb5079068aeef436fc7f39526e3664f44d9b10320d0`는 content authentication을 통과했습니다.
  - `service.py::_execute_ready_step`의 inspect가 deep-read 대신 FTS5 snippet 10개만 만들고 요청 밖 파일, line range null, incomplete 결과를 반환함을 실제 run artifact와 코드에서 확인했습니다. GPT 검토는 이 증거 부족 때문에 제품 결함 주장을 조사 후보로 내렸습니다.
  - 동일 내용의 `prepare_egress`를 두 번 실행해 timestamp 때문에 fact-pack/manifest digest가 달라짐을 재현했습니다.
  - declared fallback 미반영, 검증 없는 성공의 quality 1.0 기록, step `input_artifact_ids` 미갱신, 병렬 elapsed 합산 time budget을 코드에서 확인했습니다.
  - worker의 광범위한 env/cwd 상속과 직계 process만 취소하는 경계, 임시 빈 DB health와 실행 파일 전환만 결합한 release rollback 위험을 확인했습니다.
- **미완**: 이번 감사에서 위 결함은 구현 수정하지 않았습니다. final LLM artifact는 불완전한 fact pack을 입력으로 받았으므로 제품 백로그 정본이 아닙니다.
- **변경 파일**: 감사 종료 시 `HANDOFF.md` managed block만 갱신합니다. bootstrap 변경은 commit `326ff0a`에 있습니다.
- **검증 실행 결과**: bootstrap 집중 회귀 `5 passed`; 전체 pytest `495 passed, 2 skipped`; Ruff; bash syntax; 문서 품질; user-facing verify; Ruler sync; plugin check; version sync; build; `git diff --check`를 통과했습니다. 감사 run revision 22 completed, final artifact `content_authenticated=true`입니다.
- **현재 리스크**: deep inspect가 실제 파일·줄 근거를 보장하지 않습니다. routing 품질 학습이 호출 성공을 과대평가할 수 있습니다. worker filesystem/environment 및 process-group 취소 경계가 약합니다. schema bump 뒤 executable-only rollback은 구버전 복구를 보장하지 못할 수 있습니다.
- **Do-Not-Repeat**: FTS5 snippet을 deep read나 `path:line` 증거로 표현하지 마세요. `non_empty` 검증을 품질 통과로 표현하지 마세요. active lease 요청을 중복 continue/retry하지 마세요. LLM 조사 후보를 재현 없이 확정 결함으로 승격하지 마세요.
- **다음 한 걸음**: `src/agent_hub/v2/service.py::_execute_ready_step`, `src/agent_hub/v2/context.py::search_fact_pack`, `src/agent_hub/v2/store.py::search_context`와 관련 테스트를 수정해 명시적 파일·심볼 범위를 complete fact pack과 실제 line range로 수집하고 누락·절단 시 inspect verification이 실패하도록 구현하세요.
<!-- agent-hub:handoff:v1:end -->
