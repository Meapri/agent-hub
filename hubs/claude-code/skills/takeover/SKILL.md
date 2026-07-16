---
name: takeover
description: >
  HANDOFF.md를 읽고 진행 중이던 작업을 이어받는다. 새 세션이나 다른 하네스에서 작업을 재개할 때 실행.
  Trigger when the user says "takeover", "이어받아", "이어서", "resume handoff", or when opening work
  that already has a HANDOFF.md.
---

## Steps
1. 작업 루트의 `HANDOFF.md`를 읽는다. 없으면 사용자에게 위치를 묻는다.
2. **먼저 "Do-Not-Repeat"를 확인**하고 반드시 준수한다 — 이미 실패한 걸 반복하지 않는다.
3. "변경 파일" + 첨부 diff + "검증 실행 결과"로 현재 상태를 복원한다. 필요하면 shared memory(`memory` MCP 서버)에서
   관련 결정·교훈을 조회한다.
4. `git status --short`로 다른 에이전트가 같은 파일을 만지고 있지 않은지 확인한다 — **한 번에 한 에이전트만 코드를 쓴다.**
5. 복원한 상태를 사용자에게 한 문단으로 요약한 뒤, "다음 한 걸음"부터 착수한다.
6. 원 대화 컨텍스트를 요구하지 말 것 — HANDOFF.md와 저장소 상태만으로 이어갈 수 있어야 정상이다.
