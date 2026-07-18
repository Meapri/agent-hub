---
name: document-write
description: >
  코드와 설정을 근거로 README나 장기 보존 기술 문서를 새로 쓰거나 전면 개편한다.
  Trigger when the user asks to write or rewrite a repository README, public Korean documentation,
  setup guide, or durable technical document with Agent Hub.
---

# 문서 작성

읽는 사람이 기능과 사용법을 한 번에 이해할 수 있는 문서를 만든다. 문장을 그럴듯하게 꾸미는 일보다
빠진 기능, 제약, 설정, 실행 방법이 없는지 확인하는 일을 먼저 한다.

## 진행 순서

1. 대상 저장소의 `AGENTS.md` 또는 `CLAUDE.md`, 현재 README, 주요 진입점, 공개 도구와 스키마, 설정,
   대표 테스트, Git 상태를 확인한다.
2. 저장소 전체를 설명하는 문서라면 먼저 `agent_hub_plan_workflow`의 `adaptive` workflow로 계획만
   만든다. 이 도구는 실행기가 아니다. 반환된 plan에서 `inspect_codebase`가 작성 단계의 의존성인지,
   넓은 문서에 `investigation_depth=deep`, `reasoning_effort=high`가 필요한지 확인한다. 검토한 plan은
   wave가 적은 plan은 `agent_hub_run_workflow`에 그대로 넘긴다. 여러 wave가 필요한 plan은
   `agent_hub_start_workflow`에 넘긴 뒤 반환된 `run_id`로 `agent_hub_continue_workflow`를 반복한다.
   end-to-end 실행 결과가 `timed_out`이더라도 `resumable=true`이면 새로 시작하지 말고 그 `run_id`를
   이어간다. 중간 상태의 문서 텍스트는 최종 파일로 저장하지 않는다.
3. 작은 문서나 일부 문단 수정은 `agent_hub_write`를 직접 사용할 수 있다. README에는 `task=readme`,
   절대 `project_root`, `policy_mode=required`를 넘긴다. `source_file`은 절대경로로 주거나,
   `project_root` 또는 `workspace_root` 안의 상대경로로 준다.
4. 생성 결과의 `quality_gate.applied`와 `quality_gate.passed`를 확인한다. `passed=false`이거나 도구 응답이
   실패했다면 결과를 파일에 쓰지 않는다. placeholder나 저장소에 없는 파일 경고도 차단 오류로 취급하고,
   경고를 반영해 전체 문서를 다시 만든다.
5. 파일을 바꾼 뒤 `agent_hub_verify`를 `doc_class=durable`, `user_facing=true`로 실행한다. 저장소에
   `orchestrate_codex.document_quality`가 있으면 대상 파일에 대한 로컬 검사도 실행한다.
6. 문서에 적은 명령과 주요 기능을 코드·테스트와 다시 대조하고, 저장소의 동기화 검사와 관련 테스트를
   실행한다.

## 한국어 원칙

- 한국 개발자가 다른 사람에게 제품을 설명하듯 자연스럽고 단정한 존댓말로 쓴다.
- 영어 문장 구조를 그대로 옮기거나 추상 명사를 연달아 붙이지 않는다. 익숙한 한국어와 짧고 구체적인
  문장을 우선한다.
- 본문을 `~한다`, `~이다`로 계속 끝내는 독백체는 쓰지 않는다. 명령은 `~하세요`, 설명은 상황에 맞는
  자연스러운 존댓말을 사용한다.
- `콕핏`, `substrate`, `conductor`, `provider leaf` 같은 내부 용어를 설명 없이 노출하지 않는다.
  공식 명칭이나 코드 식별자는 그대로 두되, 처음 나올 때 쉬운 말로 역할을 설명한다.
- “먼저 살펴보겠습니다”, “이를 통해 활용할 수 있습니다”처럼 작업 과정을 중계하거나 번역한 듯한
  문장은 빼고 사용자가 알아야 할 내용부터 쓴다.
- 쉽게 쓴다는 이유로 기능, 제약, 설치 단계, 명령, 검증 결과를 빼지 않는다.

## 실패 처리

- 품질 검사를 통과하지 못한 결과는 초안으로만 취급한다.
- 호출 횟수를 다 썼거나 provider가 실패했다면 완성된 문서처럼 저장하지 말고 실패 원인과 남은 경고를
  사용자에게 보여 준다.
- 확인하지 못한 기능과 명령은 추측해서 채우지 않는다.
