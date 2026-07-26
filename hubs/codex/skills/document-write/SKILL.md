---
name: document-write
description: >
  코드와 설정을 근거로 README나 장기 보존 기술 문서를 새로 쓰거나 전면 개편한다.
  Trigger when the user asks to write or rewrite a repository README, setup guide, or durable
  technical document with Agent Hub.
---

# 문서 작성

문장을 꾸미기 전에 코드 근거와 빠진 기능, 제약, 설치·검증 방법을 확인한다.

## 진행 순서

1. 대상 저장소의 정책 파일, 현재 문서, entrypoint, 공개 schema, 설정, 대표 테스트와 Git 상태를
   확인한다.
2. 저장소 전체를 설명하는 문서는 `agent_hub_plan(mode="prepare")`로 fact pack과 egress manifest를
   만든다. 조사할 하위 시스템, 파일, 명령, 심볼을 `source_paths`와 task constraint에 구체적으로 적는다.
3. `approval_mode="manual"`이면 제안된 파일·분류·예산을 연결 GUI에서 사용자가 승인하게 한다.
   `approval_mode="automatic"`이고 review 상태가 `approved`이면 전역 자동 승인 기록을 사용한다.
   반환된 `approval_request.review_id`를 `approval_request_id`로 전달해
   `agent_hub_plan(mode="apply")`를 호출한다. 계획에는 조사, 작성, 검토, deterministic
   verifier의 의존 관계가 드러나야 한다.
4. 승인된 계획은 `agent_hub_start`와 revision-fenced `agent_hub_continue`로 실행한다. 중간 텍스트는
   문서로 저장하지 않고 완료된 `agent_hub_artifact`의 digest와 provenance를 먼저 확인한다.
5. 저장소 자료를 추가로 보내지 않는 작은 inline 초안은 `agent_hub_execute(capability="write")`로
   만들 수 있다. 저장소 파일, HANDOFF, 기존 artifact를 포함하면 반드시 plan prepare/apply 경로를 쓴다.
6. 호스트가 파일을 적용한 뒤 `agent_hub_artifact(action="verify")`, 저장소의
   `orchestrate_codex.document_quality`, 관련 pytest와 동기화 검사를 실행한다.
7. 검증 결과와 사용자 채택 여부를 `agent_hub_feedback`으로 기록한다. 실패하거나 검증되지 않은 결과는
   완성본으로 저장하지 않는다.

## 한국어 원칙

- 한국 개발자가 다른 사람에게 제품을 설명하듯 자연스럽고 단정한 존댓말로 씁니다.
- 영어 문장 구조를 그대로 옮기거나 추상 명사를 연달아 붙이지 않습니다.
- 본문을 `~한다`, `~이다`로 계속 끝내는 독백체를 피합니다.
- 내부 코드 식별자는 유지하되 낯선 내부 용어는 처음 나올 때 쉬운 말로 설명합니다.
- 작업 과정을 중계하는 문장보다 사용자가 알아야 할 기능, 제약, 명령과 검증 근거를 먼저 적습니다.

## 실패 처리

- `outcome_unknown`, provider 실패, artifact 검증 실패, placeholder, 존재하지 않는 파일 주장은 차단
  오류로 취급한다.
- 확인하지 못한 기능, 명령, 환경변수와 테스트 결과를 추측해 채우지 않는다.
- 원문 prompt, credential, raw provider error를 문서나 HANDOFF에 복사하지 않는다.
