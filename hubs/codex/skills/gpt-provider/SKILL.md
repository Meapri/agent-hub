---
name: gpt-provider
description: >
  공식 Codex 구독 로그인으로 Agent Hub의 canonical GPT provider를 사용하거나 준비 상태를 진단한다.
  Trigger when the user asks to use GPT, ChatGPT, Codex models, or the Codex subscription login
  through Agent Hub.
---

# GPT Provider

GPT는 별도 MCP나 별도 오케스트레이터가 아니라 Agent Hub provider registry의 `gpt` 항목이다.

## 사용 경로

1. `agent_hub_status`와 `provider="gpt"`로 동의·로그인·준비 상태를 확인한다.
2. 모델 목록은 `agent_hub_list_models`, 대화는 `agent_hub_chat`에 `provider="gpt"`를 지정한다.
3. compare·fixed/adaptive workflow에서도 같은 provider ID를 사용한다. provider를 생략한 기본 compare는
   Claude·Grok·Gemini만 실행하며, 명시적 `provider="all"`은 GPT도 포함한다.
4. 기본 로컬 점검에는 `agent-hub-doctor`를 사용한다. 공식 Codex 계정의 redacted 상태까지 확인해야 할 때만
   `agent-hub-doctor --live`를 실행한다.
5. 동의가 없으면 로컬 `openai-codex-consent` 명령을 안내한다. 로그인이 없으면 사용자가 직접
   `codex login` 또는 `codex login --device-auth`를 실행하게 한다.

## 경계

- `openai_codex_*`는 Hub 내부 adapter 도구명이다. 이를 별도 MCP로 등록하거나 public tool처럼 호출하지 않는다.
- Agent Hub는 Codex OAuth token, Keychain 항목, `auth.json`을 읽거나 복사하지 않는다.
- 로그인·refresh는 설치된 공식 Codex CLI가 소유한다. Hub가 공유 계정을 logout하지 않는다.
- GPT adapter는 격리된 read-only `codex exec`만 사용한다. shell·파일 변경·MCP·web search event가
  나타나면 결과를 실패 처리한다.
- GPT는 text chat·write·compare·review와 local image 입력을 지원한다. search, image generation,
  remote image URL은 지원한다고 가정하지 않는다.
