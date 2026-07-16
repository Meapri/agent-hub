# Model-access / orchestration 실작동 증거 (E1)

이 디렉토리는 agent-hub의 모델접근·오케스트레이션 층이 **실제로 end-to-end 작동함**을 증명하는
런타임 아티팩트를 정본에 편입한다. 실체 코드는 `../leaves.manifest.json`이 가리키는 외부 published
레포에 있고, 이 증거는 "작동하지만 git 밖"이던 상태(2026-07-16 재기획 §0.0의 custody 리스크)를 해소한다.

## orchestrate-run-970c19b355f8.json

- **출처**: `~/.orchestrate_codex/runs/970c19b355f8.json` (orchestrate-codex 런타임, version 0.4.0)
- **레시피**: `deep_readme` — `status=completed`
- **참여 leaf (전부 completed, attempt 0)**:
  - `claude_codex_chat` (Anthropic Claude) — investigate_arch
  - `grok_codex_chat` (xAI Grok) — investigate_usage
  - `google_antigravity_write` (Google Gemini via Antigravity) — draft
- **산출물**: `/Users/naen/Git/Claude Codex`(사용자 본인의 공개 플러그인 레포)의 한국어 README 초안 +
  `verify` 통과(`ok=true`, 경고 3건은 fact-pack 밖 tool 언급 수준). 즉 세 provider가 협업해 문서 1건을 완성했다.
- **작동 일시**: 2026-07-16 14:33 (KST)

## leaves.json

- **출처**: `~/.orchestrate_codex/leaves.json` (off-git 런타임 배선)
- 세 leaf MCP의 로컬 실행 커맨드/경로 정의. R6에서 이 배선을 `hubs/claude-code/.mcp.json`로 정본화한다.

## 커밋 전 안전 점검 (2026-07-16)

- **secret 스캔**: `grep -inE 'sk-…|AIza…|gh[pousr]_…|xai-…|Bearer …|-----BEGIN|"(api_key|access_token|refresh_token)":"…"'`
  → **매치 0건**(exit 1). 파일 내 `sk-ant-...` 유사 텍스트는 생성된 README 초안 속 플레이스홀더로 정규식에 미포함.
- **내용 검토**: run 본문은 사용자 본인의 **공개** 레포(`github.com/Meapri/claude-codex`) README 초안과 그 소스
  컨텍스트다. 비공개/개인/고객 데이터 없음 → truncation 불필요, 원본 그대로 편입.

## R3 — claude-codex 과금 경로 판정 (2026-07-16)

- **판정: 구독 plan-lane (소거법).** 계정에 pay-as-you-go **API 크레딧이 0**인 상태에서 orchestrate run
  `970c19b355f8`의 `claude_codex_chat`이 **정상 완료**됐다. 종량 API(`x-api-key`) 경로였다면 크레딧 부족으로
  실패했을 것이므로, 호출은 구독 OAuth plan-lane으로 처리된 것이다. 조용한 종량 과금 fall-through 위험은 해소.
- **ToS 리스크 — 결정: 수용 (2026-07-16, 사용자).** claude-codex는 구독 quota를 쓰기 위해 요청에 'Claude Code'
  정체성 fingerprint를 주입한다(공식 CLI 모방). leaf의 `NOTICE.md`가 밝힌 Anthropic 구독 ToS 위반 소지가
  있고 감지 시 계정 영향 가능성이 있으나, **사용자가 본인 계정·구독 기준으로 위험을 인지하고 계속 사용하기로
  결정**했다. 위험 발생 시 대비책은 `CLAUDE_CODEX_AUTH_MODE=api_key` fallback(크레딧 필요).

## 재현 참고

run 상태는 총 7개(2026-07-16 14:07~14:33, version 0.4.0): completed 4, failed 1, running(중단 잔류) 2.
이 파일은 completed 4개 중 세 provider가 모두 참여한 대표 run이다.
