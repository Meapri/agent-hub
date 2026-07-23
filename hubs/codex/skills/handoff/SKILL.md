---
name: handoff
description: >
  현재 프로젝트의 작업 상태를 프로젝트별 HANDOFF.md 복구 기록으로 안전하게 갱신한다.
  Trigger when the user says "핸드오프", "hand off", "인계", "넘겨", or before switching
  agents/models or ending a session.
---

# 프로젝트 핸드오프

핸드오프의 정본은 대화나 MCP memory가 아니라 현재 프로젝트의 Git에 남는 `HANDOFF.md`다. 다른
하네스가 원 대화 없이 다음 행동을 실행할 수 있을 만큼만 복구 정보를 남긴다.

## 진행 순서

1. 현재 작업의 절대 `project_root`를 확인한다. `git status --short`, 관련 diff, 최근 커밋과 이번
   세션에서 실제로 실행한 검증 결과를 모은다.
2. `agent_hub_get_handoff`를 `project_root`, `mode=auto`, `search=nearest`로 호출한다. monorepo 하위
   프로젝트에 자체 `HANDOFF.md`가 있으면 그것을 우선하고, 없을 때만 같은 Git 저장소의 가까운 상위
   파일을 사용한다. 다른 저장소나 형제 프로젝트의 파일을 가져오지 않는다.
3. 아래 내용을 짧은 Markdown 패킷으로 만든다.
   - 원래 목표와 현재 단계
   - 완료와 미완
   - 실제 변경 파일
   - 실제 검증 결과
   - 현재 위험과 반복하면 안 되는 실패
   - 이어받는 쪽이 바로 실행할 단 하나의 `다음 한 걸음`
4. `agent_hub_prepare_handoff_update`에 패킷을 `body`로 넘긴다. 기본값은 현재
   `project_root/HANDOFF.md`를 만들거나 갱신한다. 상위 프로젝트 파일을 의도적으로 갱신할 때만
   `search=nearest`나 명시적인 `file`을 사용한다. 반환된 `target`, `expected_sha256`, `content`를
   확인한다. 이 단계에서는 파일이 바뀌지 않는다.
5. 대상과 diff가 맞을 때만 `agent_hub_apply_handoff_update`에 같은 `project_root`, `target`을
   `file`로, 준비된 `content`와 `expected_sha256`을 넘긴다. SHA 충돌이면 다른 변경을 덮어쓰지 말고
   파일을 다시 읽어 패킷을 재준비한다.
6. 적용 뒤 `git diff -- HANDOFF.md`와 `git status --short`를 확인한다. stage·commit·push는
   사용자가 요청했을 때만 관련 파일로 범위를 좁혀 수행한다.

## 경계

- 진행 상태와 결정은 `HANDOFF.md`에 남긴다. shared memory는 선택적인 검색 보조일 뿐 정본이 아니다.
- 비밀값, 개인 데이터, provider 토큰, 대화 전문은 넣지 않는다.
- 실행하지 않은 테스트를 통과했다고 쓰지 않는다.
- 전체 작업 트리를 한꺼번에 stage하는 명령을 사용하지 않는다.
- Agent Hub의 marker 밖 기존 기록은 보존한다.
