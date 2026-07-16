---
name: route-to
description: >
  현재 하위 작업을 다른 provider 모델(Claude/Grok/Gemini)이나 다단계 오케스트레이션에 위임하고 결과를
  회수한다. Trigger when the user wants a second opinion, a specific provider, cross-model review, a
  multi-step doc/change recipe, or says "Claude/Grok/Gemini에게 물어봐", "다른 모델로 돌려봐".
---

## 위임 경로 (provider leaf + orchestrate-codex conductor)

모델접근은 provider별 **leaf 플러그인**(claude-codex / grok-codex / google-antigravity-codex)으로,
다단계 조율은 **orchestrate-codex** conductor로 한다. (BUILD-SPEC §0.0 확정 아키텍처. 과거 PAL MCP는 은퇴.)
Codex에선 host(GPT)가 지휘자이고, leaf/conductor는 별도 설치한 Codex 플러그인 MCP다.

- **단발 위임 (leaf 직접 호출)**: `claude_codex_chat`, `grok_codex_chat`,
  `google_antigravity_chat`/`google_grounded_search`/`google_antigravity_write`.
  각 leaf는 자기 **consent gate + 구독 OAuth**를 강제한다.
- **다단계 위임 (conductor)**: `orchestrate_advise`(라우팅 브리핑) → `orchestrate_step`(위임 준비, 최신
  모델·컨텍스트·정책 주입) → `orchestrate_verify`(가드). 레시피는 `orchestrate_start_run` →
  `orchestrate_continue_recipe`. opt-in `orchestrate_run`은 broker가 leaf를 직접 스폰해 끝까지 실행.

## Steps
1. 단발이면 알맞은 leaf 하나를, 다단계면 `orchestrate_advise`로 계획을 먼저 받는다.
2. 위임 프롬프트엔 필요한 컨텍스트(파일 경로·목표·제약)만 담는다 — 과대 패킷 금지.
3. leaf 호출은 **provider 과금/구독 소진 상태 변경**이다. 대량·반복 호출 전엔 목적을 분명히 한다.
4. 회수한 결과를 원 작업에 반영하고, 중요한 결정·교훈은 shared memory(`mcp__memory__*`)에 기록한다.
