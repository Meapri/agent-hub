---
name: route-to
description: >
  현재 하위 작업을 Claude, Grok, Gemini, GPT 중 지정한 provider나 지능형 오케스트레이터에 위임한다.
  Trigger when the user asks for a specific provider, a second opinion, cross-model review, or says
  "Claude/Grok/Gemini/GPT에게 물어봐", "다른 모델로 돌려봐".
---

# Provider 위임

모든 provider 접근은 Agent Hub v2의 동일한 task 계약을 사용한다.

## 위임 경로

- 짧은 inline 요청은 `agent_hub_execute`에 capability와 `provider`를 명시한다.
- 저장소 파일이나 기존 artifact가 필요한 작업은 `agent_hub_plan` prepare 뒤 `approval_mode`를
  확인한다. `manual`이면 연결 GUI에서 사용자가 승인하고, `automatic`이면 승인된 review를 사용해
  반환된 `approval_request_id`로 apply한 뒤 `agent_hub_start`로 실행한다.
- provider를 고정하기 전 `agent_hub_catalog`에서 model capability와 auth/catalog/generation 상태를
  확인한다.
- 결과 본문은 `agent_hub_artifact`, 진행 상태는 `agent_hub_events`로 분리해 읽는다.

## 실행 원칙

1. 사용자 요청에 특정 provider가 있으면 그 선택을 유지하고 fallback 여부를 constraint에 명시한다.
2. provider를 지정하지 않은 복잡한 작업은 planner와 router가 정책 범위 안에서 정하게 한다.
3. 저장소나 artifact를 보낼 때는 승인된 egress manifest 이상으로 컨텍스트를 추가하지 않는다.
4. 결과의 실제 provider/model, routing mode, 표본 수, verification과 artifact digest를 보고한다.
5. 모델 호출은 구독이나 API 사용량을 소모한다. 대량 반복이나 opt-in canary는 사용자 승인 범위에서만
   실행한다.

## 경계

- connected, live catalog, verified generation을 같은 상태로 취급하지 않는다.
- placeholder나 내부 model ID를 generation 요청에 전달하지 않는다.
- prompt, output, credential, raw exception을 event나 HANDOFF에 복사하지 않는다.
- LLM 자기 평가만으로 provider 품질을 학습시키지 않는다.
