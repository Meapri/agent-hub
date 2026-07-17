---
name: route-to
description: >
  현재 하위 작업을 다른 provider 모델(Claude/Grok/Gemini)이나 다단계 오케스트레이션에 위임하고 결과를
  회수한다. Trigger when the user wants a second opinion, a specific provider, cross-model review, a
  multi-step doc/change recipe, or says "Grok/Gemini에게 물어봐", "다른 모델로 돌려봐".
---

## 위임 경로

모든 모델 접근과 다단계 조율은 `agent-hub` MCP 하나를 사용한다.

- **단발 위임:** `agent_hub_chat`, `agent_hub_search`, `agent_hub_write`,
  `agent_hub_generate_image` 중 작업에 맞는 도구를 호출한다.
- **다단계 위임:** `agent_hub_plan_workflow`로 계획을 확인한 뒤
  `agent_hub_start_workflow`/`agent_hub_continue_workflow`를 사용하거나,
  `agent_hub_run_workflow`로 끝까지 실행한다.
- **검증:** 결과는 `agent_hub_verify`로 확인한다.
- provider별 동의와 OAuth 검사는 내부 adapter가 계속 강제한다.

## Steps
1. 단발이면 알맞은 `agent_hub_*` 도구를, 다단계면 workflow 계획을 먼저 받는다.
2. 위임 프롬프트엔 필요한 컨텍스트(파일 경로·목표·제약)만 담는다 — 과대 패킷 금지.
3. 모델 호출은 **provider 과금/구독 소진 상태 변경**이다. 대량·반복 호출 전엔 목적을 분명히 한다.
4. 회수 결과를 원 작업에 반영하고, 중요한 결정·교훈은 shared memory(`memory` 서버)에 기록한다.
