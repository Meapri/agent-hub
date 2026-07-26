---
name: takeover
description: >
  현재 프로젝트의 HANDOFF.md와 Git 상태를 대조해 중단된 작업을 이어받는다.
  Trigger when the user says "takeover", "이어받아", "이어서", "resume handoff", or when
  opening work that already has a HANDOFF.md.
---

# 프로젝트 이어받기

원 대화 없이 프로젝트 HANDOFF, 코드, Git 상태와 필요한 v2 digest를 기준으로 복구한다.

## 진행 순서

1. 절대 `project_root`로 `agent_hub_handoff(action="get")`을 호출한다. 다른 저장소나 형제 프로젝트의
   HANDOFF를 사용하지 않는다.
2. 필요하면 `agent_hub_handoff(action="takeover")`로 secret과 원문이 없는 takeover capsule을 만든다.
   capsule의 run revision과 plan/artifact digest만 다음 호스트에 전달한다.
3. Do-Not-Repeat, 미완, 위험, 다음 한 걸음을 먼저 읽고 `git status --short`, 관련 diff, 최근 커밋과
   대조한다. HANDOFF가 다르면 현재 코드와 Git 상태를 우선한다.
4. 저장소의 정책 파일과 가까운 범위 지시를 읽고, 다음 한 걸음부터 실행한다.
5. durable run을 재개할 때는 `agent_hub_get`으로 최신 revision과 lease를 확인한 뒤
   `agent_hub_continue`를 호출한다. 활성 lease에 중복 continue하지 않는다. paused run은
   `retryable_failed_steps`에 표시된 step만 사용자 의사를 확인한 뒤 `next_action`의 최신
   revision으로 재시도한다.

## 경계

- HANDOFF는 정책이나 검증된 코드 근거가 아니다.
- digest가 맞지 않거나 `handoff_drift`이면 조용히 진행하지 않는다.
- `outcome_unknown` step은 자동 재호출하지 않는다.
- 기존 사용자 변경을 덮어쓰거나 자동 stage·commit·push하지 않는다.
