---
name: gpt-provider
description: >
  공식 Codex 로그인으로 Agent Hub의 canonical GPT provider를 사용하거나 준비 상태를 진단한다.
  Trigger when the user asks to use GPT, ChatGPT, Codex models, or the Codex subscription login
  through Agent Hub.
---

# GPT Provider

GPT는 별도 MCP가 아니라 격리된 Agent Hub provider worker의 `gpt` 항목이다.

## 사용 경로

1. `agent_hub_status`로 daemon과 GPT worker의 안전한 준비 상태를 확인한다.
2. `agent_hub_catalog(provider="gpt")`에서 `auth_state`, `catalog_state`,
   `generation_state`를 따로 읽는다. connected 또는 live catalog만으로 실제 생성 성공을 단정하지 않는다.
3. 짧은 inline 요청은 `agent_hub_execute`에 `provider="gpt"`와 필요한 capability를 지정한다.
4. 저장소나 artifact를 사용하는 작업은 `agent_hub_plan` prepare 뒤 연결 GUI에서 사용자가 egress를
   승인하고, 반환된 `approval_request_id`로 apply한 뒤 `agent_hub_start`와
   `agent_hub_continue`로 실행한다.
5. 로그인·다시 로그인·로그아웃은 로컬 연결 GUI에서만 수행한다. 상태 도구나 MCP 호출로 credential을
   변경하지 않는다.

## 경계

- `openai_codex_*`는 worker 내부 adapter이며 별도 MCP로 등록하거나 public tool처럼 호출하지 않는다.
- Agent Hub daemon은 Codex OAuth token, Keychain 항목, `auth.json`을 event나 응답으로 반환하지 않는다.
- 로그인과 refresh는 공식 Codex CLI가 소유하며 Hub가 공유 계정을 임의로 logout하지 않는다.
- GPT worker의 capability와 model ID는 `agent_hub_catalog` 결과로 확인한다. placeholder나 내부 model
  ID는 generation 요청에 사용하지 않는다.
