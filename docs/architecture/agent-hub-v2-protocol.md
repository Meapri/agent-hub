# Agent Hub v2 protocol

이 문서는 daemon, MCP bridge, provider worker를 서로 독립적으로 구현할 때 필요한 고정 경계입니다.
정규화와 digest 계산의 실행 기준은 `agent_hub.v2.contracts`, 배포 가능한 schema fixture는
`agent_hub/v2/schemas/contracts.json`입니다.

## Process boundaries

- MCP bridge는 NDJSON이 아니라 MCP JSON-RPC를 stdio로 받고, 한 요청씩 사용자 전용 Unix socket에
  전달합니다.
- daemon socket 요청은 `{"id", "method", "params"}` 한 줄이며 `ping`, `tools/list`, `tools/call`만
  허용합니다.
- provider worker는 stdin/stdout NDJSON 한 줄 요청·응답을 사용합니다. method는 `initialize`,
  `status`, `catalog`, `invoke`, `plan`, `cancel`, `shutdown`입니다.
- worker stdout에는 응답 JSON 한 개만 쓸 수 있습니다. credential, prompt, 결과 원문, stderr와 raw
  exception은 event나 error envelope로 전달하지 않습니다.

## Durability

외부 요청 직전 step은 `running`과 request digest를 같은 transaction에 기록합니다. daemon이
응답 commit 전에 종료되면 retry-safe local step만 `queued`로 회수합니다. 외부 step은
`outcome_unknown`으로 고정하며 사용자가 결과를 조정하기 전에는 claim할 수 없습니다.
provider가 명확한 retryable 오류를 반환해 `failed`가 된 step만 최신 run revision과
`retry_failed_steps` 목록으로 명시적으로 `queued`에 되돌릴 수 있습니다. 재시도는 step 상태,
retry-safe checkpoint와 lease 부재를 하나의 transaction에서 다시 확인합니다.
`agent_hub_get`은 해당 step ID와 바로 사용할 수 있는 revision-fenced `next_action`을
반환합니다. 취소는 run과 아직 완료되지 않은 step을 한 transaction에서 `cancelled`로 바꾸며,
뒤늦게 도착한 worker 결과는 step output으로 연결하지 않습니다.

`outcome_unknown` step의 조정은 `agent_hub_cancel`의 `prepare_reconcile`과 `apply_reconcile`
두 단계입니다. 판정은 `not_delivered`, `delivered_discarded`, `delivered_recovered` 세 가지이고
외부 재전송을 유발하는 것은 `not_delivered` 하나뿐입니다. apply는 proposal digest, run
`expected_revision`, 준비 시점 step revision·attempt·request digest로 만든 `witness_sha256`,
그리고 일회성 confirmation phrase를 함께 확인합니다. phrase는 재전송이면 `resend-`, 아니면
`discard-`로 시작합니다. `agent_hub_get`의 `next_action`은 재전송하지 않는 판정만 미리 채우므로
그대로 실행해도 외부 요청이 다시 나가지 않습니다. 한 run의 조정 횟수는 3회로 제한하며 모든
`outcome_unknown` step을 한 번에 판정해야 합니다. `delivered_recovered`가 제공한 텍스트도 plan이
선언한 verifier를 통과해야 하고 artifact verification에 `human_reconciliation` 출처가 남습니다.

Run token 예산은 plan digest에 봉인돼 있으므로 정책을 올려도 기존 run에는 적용되지 않습니다.
`agent_hub_continue`의 `token_budget_grant`는 plan을 바꾸지 않고 run 단위 가산값만 늘립니다.
예산 소진은 재개 가능한 종착이며 남은 step은 `queued`로 보존됩니다. Step별 입력·출력 token은
전용 컬럼에 시도 단위로 누적하고, provider가 usage를 보고하지 않으면 추정치로 채운 뒤
`tokens_source`로 실측과 추정을 구분합니다. 추정치는 routing 표본에 넣지 않습니다.

DB schema migration은 기존 DB integrity check, SQLite backup, migration, 사후 integrity check 순서로
수행합니다. 실패하면 pre-migration backup으로 복구합니다. release candidate는 현재 DB의 복사본으로
기동해 schema compatibility를 확인하며, rollback slot은 LaunchAgent와 migration 전 DB snapshot을
같이 보존합니다. 실제 전환 직전에도 emergency DB snapshot을 만들고, DB restore·candidate
bootstrap·health check가 실패하면 이전 LaunchAgent, DB, daemon을 순서대로 복구합니다. 이전 daemon
health check까지 성공해야 `release_activation_failed`를 반환하며, 보상이 불완전하면
`release_recovery_failed`와 실패 단계만 안전하게 노출합니다. 보상이 불완전한 경우 emergency DB
snapshot은 rollback slot 옆에 남기고, 후속 release switch는 해당 snapshot이 검토될 때까지
`release_recovery_pending`으로 차단합니다.
update가 실패하면 전환 직전에 존재하던 rollback plist·metadata·DB snapshot도 함께 복구해,
실패한 candidate가 이전 rollback 이력을 덮어쓰지 못하게 합니다.

## Egress and provider isolation

repository source는 `agent_hub_plan(mode="prepare")`에서 `fact_pack_v2`와
`egress_manifest_v2`를 만든 뒤 동일한 proposal·manifest·policy revision digest로만 planner에
전달합니다. manifest는 source entry와 실제 egress destination provider를 함께 고정합니다.
source entry가 있으면 daemon은 15분짜리 `egress_review_v1`을 만들고 연결 GUI에 표시합니다.
session header와 same-origin intent 검사를 통과한 GUI action만 approve/reject할 수 있습니다.
사용자 전역 `자동 승인`은 기본적으로 꺼져 있으며 daemon SQLite의 revision-fenced 설정으로만
변경합니다. 켜져 있으면 허용된 review를 즉시 승인하되 `decision_source`와 설정 revision을
남깁니다. 프로젝트의 `denied` 정책, 민감 경로 차단과 secret redaction은 자동 승인보다 먼저
적용됩니다.
`apply`는 proposal·manifest·policy revision에 묶인 `approval_request_id`를 원자적으로 한 번
소비한 뒤에만 planner를 호출합니다. MCP 공개 도구에는 review 승인 mutation을 노출하지 않습니다.
기존 artifact도 같은 manifest에 artifact ID, 원문 digest, redaction 후 전송 digest를 기록하고
실행 직전에 다시 대조합니다. inline prompt를 현재 run에서 봉인해 만든 artifact만 해당 요청의
암묵적 동의를 이어받습니다.
외부 step으로 이어지는 inspect는 digest로 고정된 source 전체를 실제 line range와 source digest로 수집하며,
source drift, 누락, planner의 source/destination 확장은 실행 전에 거부합니다.

provider worker는 macOS sandbox에서 request별 localhost proxy port 이외의 outbound TCP를 열 수
없으며, daemon의 임시 CONNECT proxy가 provider manifest에 선언된 domain만 전달합니다. worker environment는 공통 안전
변수와 해당 provider prefix만 허용하고, cwd와 TMPDIR는 request별 임시 디렉터리를 사용합니다.
filesystem write는 임시 디렉터리와 provider config/cache로 제한하며 cancel/timeout은 process group에
전달합니다.
filesystem read는 사용자 홈 전체가 아니라 실행 코드와 해당 provider의 config/cache 경로로
제한합니다. 다른 provider credential 디렉터리와 `~/.ssh` 같은 홈 하위 경로는 읽을 수 없습니다.
환경에서 지정한 provider config/cache/credential 경로는 absolute HOME strict descendant만
허용합니다. HOME, HOME 상위 경로, `/`, 상대 경로와 넓은 경로를 가리키는 symlink는 profile
생성 단계에서 거부합니다.
Python과 macOS runtime 파일은 worker 기동을 위해 읽을 수 있습니다. 직접 DNS lookup과
사용자 홈·request runtime·`/tmp` 아래 Unix socket은 거부하고, provider HTTP(S)는 request별
localhost proxy port만 허용합니다. macOS runtime에 필요한 system socket은 이 경계 밖입니다.

setup proposal은 daemon·bridge binary digest와 전체 public proposal을 canonical JSON으로
고정합니다. host config 또는 LaunchAgent 적용·활성화가 실패하면 digest가 일치하는 변경만 역순으로
복구합니다.

## Routing, provenance, and metrics

step에 fallback을 선언하면 primary와 그 fallback만 실행 후보가 됩니다. 비어 있지 않은 결과나 단순
호출 성공은 품질 점수가 아니며, 사용자 feedback이나 명시적 deterministic verifier만 quality signal로
기록합니다. 병렬 run의 time budget은 DAG critical path로 계산하고, completed step은 실제
`input_artifact_ids`와 `output_artifact_ids`를 함께 보존합니다.
`shadow`와 `advisory`는 planner provider가 capability·policy·readiness·context 검사를 통과할
때 선택을 유지합니다. 부적격 provider를 강제로 실행하지는 않습니다.
catalog가 model별 `max_input_tokens`를 제공하면 daemon은 catalog revision과 5분 TTL로 이를
캐시해 manifest fallback보다 우선합니다. 입력 context는 선택적인 task `max_input_tokens`와
유효한 model limit 중 작은 값을 넘으면 worker 호출 전에 거부합니다. `max_output_tokens`는
provider 호출 출력에만, `max_total_tokens`는 run 누적 사용량에만 적용합니다. 이전
`max_tokens`는 output과 total의 호환 별칭이며 input limit으로 사용하지 않습니다.
dependency artifact가 `fact_pack_v2` 모양이어도 현재 run에서 완료된 `local inspect` step이
생성했다는 artifact·plan·step provenance가 모두 맞을 때만 구조화 병합합니다. Provider 출력과
이전 run의 입력 artifact는 원문 text segment로 보존합니다.

`routing_profile`은 score 가중치를 결정하며 `quality_balanced`, `latency_first`, `cost_first`만
허용합니다. 사용자 prior는 `~/.agent-hub/routing_prior.toml`에 두고 `routing_samples`에는 절대
기록하지 않습니다. prior는 pseudo-weight로 관측 통계와 Bayesian shrinkage 합성되며 지분은
`w / (w + n)`이라 표본이 쌓일수록 자동 감쇠합니다. 저장소 source에는 provider 성능 수치를 넣지
않으며 seed template은 전부 `source = "unset"`이라 가중치 0입니다. `auto` 승격은 관측 표본 하한,
prior 지분 상한 0.5, 그리고 score 차이가 양쪽 표준편차 합을 넘는 분리 조건을 모두 만족해야
합니다. 선택 근거는 `routing_decision_v1`의 `evidence_kind`와 `prior_weight_fraction`에
남습니다. prior 파일이 손상돼도 routing은 prior 없이 계속 동작합니다.

`agent_hub_handoff`의 `apply_update`가 성공하면 적용된 managed block을 로컬 snapshot으로
기록합니다. snapshot은 대상별 최근 50개까지 보존하고 저장 전에 secret 후보 줄을 제외하며,
snapshot 기록 실패가 이미 완료된 파일 쓰기를 되돌리지는 않습니다. `history`와 `diff`는 순수
읽기이며 `diff`는 아홉 개 필수 구간 단위로 비교합니다. `target_sequence` 없이 호출하면 현재
파일과 비교해 Agent Hub 밖에서 이뤄진 수정을 드러냅니다.
긴 provider 호출은 content-free `provider_attempt_started`, `provider_attempt_failed`,
`provider_attempt_completed` event로 관찰할 수 있습니다. event에는 prompt와 결과 본문을
기록하지 않습니다.

operation metric은 이름, 성공 여부, duration과 실패 코드를 최대 20,000건·90일 보존하고 요약은
최근 10,000건을 사용합니다. 실패 코드는 `^[a-z][a-z0-9_]{0,63}$`에 정확히 맞을 때만 그대로
저장하고 그 밖의 값은 `unclassified_error`로 접습니다. 실패 row는 항상 코드를 가지므로 NULL은
schema 10 이전 row만을 뜻합니다. prompt, 결과, credential, project path는 metric table에
저장하지 않습니다.

claim을 보유한 worker는 같은 claim token과 run revision으로 lease를 갱신합니다. 갱신은 revision을
증가시키지 않으며 token·revision 불일치나 이미 만료된 lease는 거부합니다. artifact retention은
row를 hard-delete하지 않고 encrypted content만 비운 metadata tombstone으로 전환해 provenance와
export audit를 보존합니다.

provider manifest는 protocol version `2.0`을 정확히 요구합니다. daemon-worker request와 response의
correlation ID가 다르면 provider protocol failure로 처리하며 외부 호출 결과가 모호한 경우
`outcome_unknown`으로 고정합니다. public task·plan·step contract는 알 수 없는 top-level field를
거부합니다. `agent_hub_execute`도 project policy 적용을 위해 절대 경로 `project_root`를 요구합니다.

## Public surface

daemon의 공개 도구는 14개로 고정합니다. 새 기능은 도구를 늘리지 않고 기존 도구의 action이나
인자로 추가합니다. provider adapter, planner backend와 저장소 구현은 worker·daemon 내부
경계이며 별도 MCP entrypoint로 노출하지 않습니다. worker는 `agent_hub.v2.provider_runtime`만
통해 provider를 호출합니다. provider adapter의 dispatch 계층은 in-process로 남아 있지만
`pyproject.toml`의 console script에는 daemon·bridge·CLI·연결 GUI만 등록하므로 host가 붙을 수
있는 MCP entrypoint는 `agent-hub-mcp` 하나뿐입니다.
