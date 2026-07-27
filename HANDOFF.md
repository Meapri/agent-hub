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
- **원래 목표**: Agent Hub의 관측 공백과 죽은 학습 루프를 메우고, 멈춘 run을 되살릴 복구 경로와 handoff 이력을 추가합니다.
- **현재 단계**: `feat/observability-and-routing-priors` 브랜치에서 우선순위 5건을 모두 구현하고 문서까지 갱신했습니다. 전체 검증을 통과했고 사용자 commit 승인을 기다립니다.
- **완료**:
  - `operation_metrics`에 `error_code`를 추가하고 `agent_hub_status`가 도구별 상위 실패 코드를 집계합니다. `HubV2Error.code`가 자유 문자열이고 provider 응답 code가 무검증 승격되므로 `normalize_error_code`가 `fullmatch`로 taxonomy 형태만 통과시키고 나머지는 `unclassified_error`로 접습니다. 실패는 항상 코드를 가지므로 NULL은 schema 10 이전 row만 뜻합니다.
  - step별 `input_tokens`/`output_tokens`/`total_tokens`/`tokens_source`를 시도 단위로 누적합니다. provider마다 다른 usage 키를 `normalize_token_usage`로 정규화하고 순수 추정치는 routing 표본에서 제외합니다. wave 시작 전 `routing_samples` 중앙값으로 예상 소비를 계산해 `run_token_budget_warning`을 남기되 차단하지 않습니다.
  - 토큰 예산 소진을 재개 가능한 종착으로 바꿨습니다. plan은 `plan_sha256`으로 봉인돼 있으므로 `runs.token_budget_limit`(plan 파생)과 `token_budget_grant`(사용자 가산)를 분리하고 `agent_hub_continue`의 `token_budget_grant`로 이어갑니다.
  - 사용자 편집 routing prior(`~/.agent-hub/routing_prior.toml`)를 추가했습니다. `routing_samples`와 분리 저장하고 Bayesian shrinkage로 합성하며 지분은 표본이 쌓일수록 자동 감쇠합니다. 저장소 source에는 provider 성능 수치가 하나도 없고 seed template은 전부 `source = "unset"`이라 가중치 0입니다.
  - `AUTO_MIN_SAMPLES` 20을 관측 하한 5로 낮추되 prior 지분 상한 0.5와 점수 분리 조건을 추가했습니다. auto가 켜지지 않던 두 번째 원인인 `context_sha256`의 model 포함은 prior의 model wildcard로 완화했습니다. `routing_profile`이 실제 가중치를 결정하게 하고 policy 검증을 붙였습니다.
  - `outcome_unknown` 조정을 `agent_hub_cancel`의 `prepare_reconcile`/`apply_reconcile`로 추가했습니다. 판정 3종 중 재전송을 유발하는 것은 `not_delivered` 하나뿐이고 확인 문구가 `resend-`로 시작합니다. `agent_hub_get`의 `next_action`은 재전송하지 않는 판정만 미리 채웁니다.
  - handoff `apply_update`가 managed block 스냅샷을 남기고 `history`와 구간별 `diff`를 읽습니다. `target_sequence` 없는 diff는 현재 파일과 비교해 Agent Hub 밖 수정을 드러냅니다. `prepare_update`의 `include_diff`로 적용 전 미리보기도 됩니다.
  - 공개 도구는 14개를 유지했습니다. 모든 신규 기능을 기존 도구의 action이나 인자로 넣었습니다. schema 9에서 10으로 올리는 단일 마이그레이션에 다섯 기능의 변경을 합쳤습니다.
- **미완**: handoff `action="list"`는 다른 프로젝트 절대경로 노출 위험 대비 가치가 낮아 제외했습니다. 로컬 설치본과 실행 중인 daemon에는 이번 변경을 적용하지 않았습니다. 멈춘 run 5개는 사용자 승인 없이 조정하지 않았습니다.
- **변경 파일**: `src/agent_hub/v2/{store.py,service.py,contracts.py,routing.py,tools.py,policy.py,dependency_context.py,metrics.py}`, 신규 `src/agent_hub/v2/routing_prior.py`, `src/agent_hub/core/handoff.py`, `src/agent_hub/v2/schemas/contracts.json`, README와 protocol 문서, adaptive-orchestrate·handoff skill 정본과 동기화 사본, 신규 테스트 `tests/agent_hub/test_v2_{routing_prior,reconciliation}.py`를 변경했습니다.
- **검증 실행 결과**: 전체 pytest `672 passed, 2 skipped`; `ruff check src tests`; `ruff format --check src tests`(176 files); `./scripts/check-sync.sh`; `./scripts/check-hub-plugins.sh`; release version 2.3.0 정합성; sdist/wheel build; README document quality와 README·protocol user-facing verify를 통과했습니다. 운영 DB 사본으로 schema 9에서 10 마이그레이션을 반복 검증해 run 17개와 integrity가 보존되고 `token_budget_limit` backfill이 채워지는 것을 확인했습니다.
- **현재 리스크**: handoff 스냅샷이 사용자 작성 본문을 `state.sqlite3`에 평문 저장하는 새 at-rest 범주를 만듭니다. 0600 권한과 저장 전 secret redaction, 50개·128KB 상한으로 완화했지만 Git 밖 사본이 생긴다는 사실은 남습니다. daemon 설정은 여전히 `auto_approve=true`이고 소스는 설치본보다 앞서 있습니다.
- **Do-Not-Repeat**: 라우팅 prior를 `routing_samples`에 섞지 마세요. 저장소 source에 provider 성능 수치를 넣지 마세요. `agent_hub_get`의 `next_action`에 재전송 판정을 미리 채우지 마세요. `requeue_failed_steps`의 UPDATE에 토큰 컬럼을 추가하지 마세요. 이미 지출된 예산이 사라집니다. `normalize_error_code`에서 `match`를 쓰지 마세요. 끝 개행이 통과합니다.
- **다음 한 걸음**: `git add -A && git commit`으로 이 브랜치의 변경과 HANDOFF를 하나의 목적 커밋으로 만든 뒤, `./.venv/bin/agent-hub stage-release --repo-root .`로 2.3.0 runtime을 staging하고 `setup --runtime-root`로 설치본을 갱신하세요.
<!-- agent-hub:handoff:v1:end -->
