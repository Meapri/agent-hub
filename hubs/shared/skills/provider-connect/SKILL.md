---
name: provider-connect
description: >
  Agent Hub의 로컬 GUI에서 Claude, Grok, Gemini, GPT 로그인, 사용 동의와 기본 모델을 관리한다.
  Trigger when the user asks to connect, sign in, disconnect, test, inspect provider accounts,
  or select/reset a provider model through a graphical setup flow.
---

# Provider Connect

인증 변경은 v2 MCP API가 아니라 사용자가 직접 확인하는 로컬 GUI에서만 수행한다.

## 사용 경로

1. `agent_hub_status`와 `agent_hub_catalog`로 provider별 `auth_state`, `catalog_state`,
   `generation_state`를 분리해 읽는다. cached/static fallback을 live catalog로, connected를 검증된
   generation으로 표현하지 않는다.
2. 개발 checkout에서는 저장소 루트의 `./.venv/bin/agent-hub-connect`를 실행한다. 설치 환경에서는
   `agent-hub-connect`의 실제 절대경로를 확인한 뒤 실행한다.
3. GUI가 열리면 사용자가 provider별 동의 범위와 계정 소유자를 확인하고 직접 로그인, 다시 로그인,
   로그아웃 버튼을 누르게 한다.
4. 모델 선택 화면에서는 live/cached/static fallback 표시를 확인한다. 선택 모델의 opt-in generation
   test는 provider 사용량을 소모하므로 사용자 동작으로만 시작한다.
5. 로그인이나 갱신 뒤 `agent_hub_status`와 `agent_hub_catalog`를 다시 읽는다. 상태 조회 자체는
   credential을 갱신하거나 generation test를 실행하지 않는다.

## 경계

- 에이전트가 사용자를 대신해 consent 체크박스를 선택하거나 OAuth를 승인하지 않는다.
- OAuth token, Keychain 내용, device code, Codex `auth.json`, 계정 식별자를 응답·로그에 노출하지 않는다.
- Claude와 GPT의 공유 로그인을 Hub가 삭제하지 않는다. Grok과 Gemini의 plugin-owned credential 삭제도
  GUI의 별도 사용자 확인 뒤에만 수행한다.
- GUI는 `127.0.0.1`, exact same-origin, session nonce 검사를 유지한다.
- 안전한 진단에는 상태 코드와 revision만 포함하고 prompt, output, credential prefix, raw exception을
  넣지 않는다.
