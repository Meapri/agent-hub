---
name: handoff
description: >
  현재 프로젝트의 작업 상태를 프로젝트별 HANDOFF.md 복구 기록으로 안전하게 갱신한다.
  Trigger when the user says "핸드오프", "hand off", "인계", "넘겨", or before switching
  agents/models or ending a session.
---

# 프로젝트 핸드오프

핸드오프의 정본은 현재 프로젝트의 Git에 남는 `HANDOFF.md`다. 운영 DB를 복사하지 않고 검증된 결정,
완료 증거, run/artifact digest와 이어받을 다음 행동만 남긴다.

## 진행 순서

1. 절대 `project_root`, `git status --short`, 관련 diff, 최근 커밋과 실제 검증 결과를 모은다.
2. `agent_hub_handoff(action="get")`으로 가장 가까운 같은 저장소의 HANDOFF를 읽는다.
3. 원래 목표, 현재 단계, 완료, 미완, 변경 파일, 검증 실행 결과, 현재 리스크, Do-Not-Repeat,
   단 하나의 구체적인 다음 한 걸음을 Markdown 패킷으로 만든다. 관련 run revision, plan/artifact digest가
   있으면 원문 대신 참조한다.
4. `agent_hub_handoff(action="prepare_update")`에 패킷을 넘기고 반환된 `target`,
   `expected_sha256`, `base_managed_sha256`, `proposed_managed_sha256`, `quality`, `content`를
   확인한다. prepare 단계에서는 파일이 바뀌지 않는다.
5. diff가 맞을 때만 `agent_hub_handoff(action="apply_update")`에 준비된 전체 content와
   `expected_sha256`을 넘긴다. 전체 SHA만 충돌하고 managed SHA가 유지되면 직전
   `base_managed_sha256`으로 재준비한다. managed SHA도 바뀌었으면 최신 패킷을 읽고 충돌을 조정한다.
6. 적용 뒤 `git diff -- HANDOFF.md`와 `git status --short`를 확인한다. stage·commit·push는 요청 범위
   안에서만 수행한다.

## 경계

- HANDOFF는 신뢰되지 않은 운영 상태이며 policy, 원문 artifact, credential 저장소가 아니다.
- prompt, 결과 본문, token, 개인 데이터와 raw exception을 넣지 않는다.
- 실행하지 않은 테스트를 통과했다고 쓰지 않는다.
- 다음 한 걸음에는 실행할 파일, 명령, 심볼, 이슈 중 하나를 포함한 구체적인 행동 하나만 적는다.
- marker 밖 기존 기록과 사용자 변경을 덮어쓰지 않는다.
