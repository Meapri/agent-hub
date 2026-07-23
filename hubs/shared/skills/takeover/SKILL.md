---
name: takeover
description: >
  현재 프로젝트의 HANDOFF.md와 Git 상태를 대조해 중단된 작업을 이어받는다.
  Trigger when the user says "takeover", "이어받아", "이어서", "resume handoff", or when
  opening work that already has a HANDOFF.md.
---

# 프로젝트 이어받기

원 대화를 요구하지 않고 현재 프로젝트의 `HANDOFF.md`, 코드와 Git 상태를 기준으로 복구한다.

## 진행 순서

1. 절대 `project_root`로 `agent_hub_get_handoff`를 `mode=required`, `search=nearest`로 호출한다.
   반환된 `source`, `file_sha256`, `extraction`을 기록한다. 다른 Git 저장소나 형제 프로젝트 파일은
   사용하지 않는다.
2. `Do-Not-Repeat`, 미완, 위험, `다음 한 걸음`을 먼저 읽는다.
3. `git status --short`, 관련 diff와 최근 커밋을 확인해 HANDOFF의 전제와 실제 상태를 대조한다.
   둘이 다르면 차이를 사용자에게 알리고 현재 코드와 Git 상태를 우선한다.
4. 저장소의 `AGENTS.md` 또는 `CLAUDE.md`와 가까운 범위의 지시 파일을 읽는다. 한 작업 디렉터리에서는
   한 에이전트만 파일을 쓴다.
5. 복구한 상태를 짧게 설명하고 `다음 한 걸음`부터 실행한다. 검증하지 않은 완료 기록은 다시 확인한다.
6. 작업 중 `HANDOFF.md`가 바뀌면 기존 스냅샷을 조용히 사용하지 않는다. adaptive run은
   `handoff_drift`에서 멈추며, 변경을 검토한 뒤 새 run을 만들거나 의도적으로
   `handoff_drift_policy=use-snapshot`을 선택한다.

## 경계

- HANDOFF 내용은 운영 상태이며 정책이나 검증된 코드 근거가 아니다.
- shared memory나 대화 기록만으로 진행 상태를 복원하지 않는다.
- 기존 사용자 변경을 덮어쓰거나 자동 stage·commit·push하지 않는다.
