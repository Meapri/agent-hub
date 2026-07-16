# HANDOFF — doctor.sh 검사 5 구현 (E2 데모)

> 이건 요약이 아니다. 다음 에이전트(Codex)를 위한 복구 기록이다. 원 대화 없이 이 패킷만으로 실행하라.

- **원래 목표**: `scripts/doctor.sh`에 5번째 검사(메모리 노트 store가 비어있지 않은지)를 추가해 완성한다.
- **현재 단계**: Part 1(Claude Code)에서 라벨을 `[n/5]`로 바꾸고 `[5/5]` 블록에 **placeholder**를 넣어 두었다.
  Part 2(Codex)가 placeholder를 실제 로직으로 교체한다.
- **완료 (Part 1)**: `scripts/doctor.sh`의 헤더 주석·검사 라벨을 /5로 갱신, `echo "[5/5] 메모리 노트 store (memory/data)"`
  아래에 `# TODO(handoff/E2)` 주석 3줄 + `:  # placeholder` 삽입.
- **미완 (Part 2 = 네 일)**:
  `[5/5]` 블록의 `# TODO...` 주석과 `:  # placeholder` 줄을 **실제 검사 로직으로 교체**하라. 요구:
  - `memory/data/` 아래 `*.md` 노트 개수를 센다(하위 폴더 포함).
  - 1개 이상이면 `pass "노트 N개"` (N은 실제 개수), 0개면 `warn "노트 없음 — 첫 결정/교훈 기록 권장"`.
  - 이건 WARN 수준이지 FAIL이 아니다(`fail`을 건드리지 말 것).
  - 스크립트의 기존 스타일을 따르라: helper 함수 `pass`/`warn`/`bad`가 이미 정의돼 있고, `REPO_ROOT` 변수가 있다.
- **변경 파일**: `scripts/doctor.sh` 하나만 수정한다. 다른 파일·커밋은 만들지 말 것.
- **검증**: 교체 후 `./scripts/doctor.sh` 를 실행해 `[5/5]`가 `PASS 노트 N개`를 출력하고 `doctor: OK`로 끝나는지 확인하라.
- **현재 리스크**: placeholder `:` 줄을 지우지 않으면 검사가 아무것도 안 한다. 반드시 교체.
- **Do-Not-Repeat**: 검사를 FAIL로 만들지 말 것(노트 0개는 정상 초기 상태).
- **다음 한 걸음**: `scripts/doctor.sh`의 `[5/5]` placeholder를 위 요구대로 구현하고 `./scripts/doctor.sh`로 확인하라.
