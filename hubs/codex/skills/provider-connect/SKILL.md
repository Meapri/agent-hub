---
name: provider-connect
description: >
  Agent Hub의 로컬 GUI에서 Claude, Grok, Gemini, GPT 로그인, 사용 동의와 기본 텍스트 모델을 관리한다.
  Trigger when the user asks to connect, sign in, disconnect, test, inspect provider accounts,
  or select/reset a provider model through a graphical setup flow.
---

# Provider Connect

provider 연결 변경은 공개 MCP 도구가 아니라 사용자가 직접 확인하는 로컬 GUI에서 수행한다.

## 사용 경로

1. 먼저 `agent_hub_status`와 `provider="all"`로 현재 동의·로그인·준비 상태를 읽는다.
2. 사용자가 GUI 연결 관리를 요청하면 선택한 provider로 `agent_hub_auth_start`를 호출해
   `provider_gui_required` 응답의 `next_action.command`와 `next_action.args`를 받는다. 이 호출은
   로그인을 시작하지 않고 현재 MCP와 같은 Python 환경의 실행 경로만 반환한다.
3. 반환된 command와 args를 그대로 실행한다. bare `agent-hub-connect`나 `python`이 PATH에 있다고
   가정하지 않는다. 도구를 쓸 수 없는 개발 checkout에서만 저장소 루트의
   `./.venv/bin/agent-hub-connect`를 사용한다.
4. GUI가 열리면 사용자가 provider별 동의 범위와 계정 소유자를 확인하고 직접 연결 버튼을 누르게 한다.
5. 로그인 뒤에는 GUI의 연결 테스트나 `agent_hub_status`로 준비 상태를 다시 확인한다. Gemini의
   GUI 연결 테스트는 사용자가 버튼을 누를 때 선택한 모델에 bounded text generation을 한 번 보내므로
   소량의 provider 사용량이 발생한다. routine `agent_hub_status`는 계속 읽기 전용이다.
6. 모델을 바꿀 때는 provider의 로컬 안전 목록을 먼저 보여 주고, 연결이 준비된 경우에만 최신 목록을
   새로고친다. 저장값을 지워도 온도, 출력 길이, 전송 방식 같은 다른 설정은 보존한다.

## 경계

- 에이전트가 사용자를 대신해 consent 체크박스를 선택하거나 동의 요청을 위조하지 않는다.
- OAuth token, Keychain 내용, device code 내부 값, Codex `auth.json`을 대화나 로그에 노출하지 않는다.
- Claude와 GPT의 공동 로그인은 Agent Hub에서 삭제하지 않는다. 연결 해제는 Hub 사용 동의만 철회한다.
- Grok과 Gemini의 plugin-owned 로컬 로그인 정보만 별도의 사용자 확인 뒤 삭제할 수 있다.
- GPT 로그인과 refresh는 공식 Codex가 계속 소유한다. GUI는 공식 CLI를 실행하고 redacted 상태만 확인한다.
- Gemini 실제 응답 테스트의 prompt, model output, provider 원문 오류를 job·GUI·로그에 복사하지 않는다.
- GUI 서버는 `127.0.0.1`에만 열고, 서버 실행마다 생성한 session nonce를 브라우저 탭의
  `sessionStorage`에서 request header로 보내며 exact same-origin 검사를 유지한다.
