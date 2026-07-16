---
name: route-to
description: >
  현재 하위 작업을 다른 provider 모델(Claude/Grok/Gemini)이나 다단계 오케스트레이션에 위임하고 결과를
  회수한다. Trigger when the user wants a second opinion, a specific provider, cross-model review, a
  multi-step doc/change recipe, or says "Grok/Gemini에게 물어봐", "다른 모델로 돌려봐".
---

## 위임 경로 (provider leaf + orchestrate-codex conductor)

모델접근은 provider별 **leaf MCP 서버**로, 다단계 조율은 **orchestrate-codex** conductor로 한다.
(BUILD-SPEC §0.0 확정 아키텍처. 과거의 PAL MCP는 은퇴.) `.mcp.json`에 등록된 서버:

- **단발 위임 (leaf 직접 호출)**: 특정 provider의 답/리뷰만 필요할 때. 허브는 네이티브 모델 그대로 운전.
  - `claude_codex` 서버 → `claude_codex_chat` (Anthropic Claude)
  - `grok_codex` 서버 → `grok_codex_chat` (xAI Grok)
  - `antigravity` 서버 → `google_antigravity_chat` / `google_grounded_search` / `google_antigravity_write` (Google Gemini)
  - 각 leaf는 자기 **consent gate + 구독 OAuth**를 스스로 강제한다. 미동의면 leaf가 거부한다.
- **다단계 위임 (conductor)**: 조사→초안→검증처럼 여러 provider를 엮을 때.
  - `orchestrate` 서버 → `orchestrate_advise`(라우팅 브리핑) → `orchestrate_step`(위임 준비) →
    `orchestrate_verify`(가드). 레시피 실행은 `orchestrate_start_run`/`orchestrate_continue_recipe`.

> Claude Code에서 플러그인 MCP 툴은 `mcp__plugin_agent-hub_<server>__<tool>` 로 네임스페이스된다
> (예: `mcp__plugin_agent-hub_orchestrate__orchestrate_advise`). 실제 이름은 툴 목록에서 확인.

## Steps
1. 단발이면 알맞은 leaf 하나를, 다단계면 `orchestrate_advise`로 계획을 먼저 받는다.
2. 위임 프롬프트엔 필요한 컨텍스트(파일 경로·목표·제약)만 담는다 — 과대 패킷 금지.
3. leaf 호출은 **provider 과금/구독 소진 상태 변경**이다. 대량·반복 호출 전엔 목적을 분명히 한다.
4. 회수 결과를 원 작업에 반영하고, 중요한 결정·교훈은 shared memory(`memory` 서버)에 기록한다.
