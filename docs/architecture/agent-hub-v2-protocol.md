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
수행합니다. 실패하면 pre-migration backup으로 복구합니다.

## Egress and provider isolation

repository source는 `agent_hub_plan(mode="prepare")`에서 `fact_pack_v2`와
`egress_manifest_v2`를 만든 뒤 동일한 proposal·manifest·policy revision digest로만 planner에
전달합니다. provider worker는 macOS sandbox에서 localhost 이외의 outbound TCP를 열 수 없으며,
daemon의 임시 CONNECT proxy가 manifest에 선언된 domain만 전달합니다.

## Compatibility

v1 데이터는 원본 JSON을 수정하지 않는 importer만 제공합니다. v1의 37개 도구가 필요한 경우
별도 `agent-hub-v1-mcp` entrypoint를 사용합니다. v2 daemon의 공개 도구는 14개로 고정합니다.
