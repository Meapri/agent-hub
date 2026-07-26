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

DB schema migration은 기존 DB integrity check, SQLite backup, migration, 사후 integrity check 순서로
수행합니다. 실패하면 pre-migration backup으로 복구합니다. release candidate는 현재 DB의 복사본으로
기동해 schema compatibility를 확인하며, rollback slot은 LaunchAgent와 migration 전 DB snapshot을
같이 보존합니다.

## Egress and provider isolation

repository source는 `agent_hub_plan(mode="prepare")`에서 `fact_pack_v2`와
`egress_manifest_v2`를 만든 뒤 동일한 proposal·manifest·policy revision digest로만 planner에
전달합니다. manifest는 source entry와 실제 egress destination provider를 함께 고정합니다.
기존 artifact도 같은 manifest에 artifact ID, 원문 digest, redaction 후 전송 digest를 기록하고
실행 직전에 다시 대조합니다. inline prompt를 현재 run에서 봉인해 만든 artifact만 해당 요청의
암묵적 동의를 이어받습니다.
외부 step으로 이어지는 inspect는 승인된 source 전체를 실제 line range와 source digest로 수집하며,
source drift, 누락, planner의 source/destination 확장은 실행 전에 거부합니다.

provider worker는 macOS sandbox에서 request별 localhost proxy port 이외의 outbound TCP를 열 수
없으며, daemon의 임시 CONNECT proxy가 provider manifest에 선언된 domain만 전달합니다. worker environment는 공통 안전
변수와 해당 provider prefix만 허용하고, cwd와 TMPDIR는 request별 임시 디렉터리를 사용합니다.
filesystem write는 임시 디렉터리와 provider config/cache로 제한하며 cancel/timeout은 process group에
전달합니다.

setup proposal은 daemon·bridge binary digest와 전체 public proposal을 canonical JSON으로
고정합니다. host config 또는 LaunchAgent 적용·활성화가 실패하면 digest가 일치하는 변경만 역순으로
복구합니다.

## Routing, provenance, and metrics

step에 fallback을 선언하면 primary와 그 fallback만 실행 후보가 됩니다. 비어 있지 않은 결과나 단순
호출 성공은 품질 점수가 아니며, 사용자 feedback이나 명시적 deterministic verifier만 quality signal로
기록합니다. 병렬 run의 time budget은 DAG critical path로 계산하고, completed step은 실제
`input_artifact_ids`와 `output_artifact_ids`를 함께 보존합니다.
긴 provider 호출은 content-free `provider_attempt_started`, `provider_attempt_failed`,
`provider_attempt_completed` event로 관찰할 수 있습니다. event에는 prompt와 결과 본문을
기록하지 않습니다.

operation metric은 이름, 성공 여부, duration만 최대 20,000건·90일 보존합니다. prompt, 결과,
credential, project path는 metric table에 저장하지 않습니다.

claim을 보유한 worker는 같은 claim token과 run revision으로 lease를 갱신합니다. 갱신은 revision을
증가시키지 않으며 token·revision 불일치나 이미 만료된 lease는 거부합니다. artifact retention은
row를 hard-delete하지 않고 encrypted content만 비운 metadata tombstone으로 전환해 provenance와
export audit를 보존합니다.

provider manifest는 protocol version `2.0`을 정확히 요구합니다. daemon-worker request와 response의
correlation ID가 다르면 provider protocol failure로 처리하며 외부 호출 결과가 모호한 경우
`outcome_unknown`으로 고정합니다. public task·plan·step contract는 알 수 없는 top-level field를
거부합니다. `agent_hub_execute`도 project policy 적용을 위해 절대 경로 `project_root`를 요구합니다.

## Public surface

daemon의 공개 도구는 14개로 고정합니다. provider adapter, planner backend와 저장소 구현은
worker·daemon 내부 경계이며 별도 MCP entrypoint로 노출하지 않습니다. worker는
`agent_hub.v2.provider_runtime`만 통해 provider를 호출하며, 이전 다중 도구 dispatch 계층이나
호환 MCP server를 포함하지 않습니다.
