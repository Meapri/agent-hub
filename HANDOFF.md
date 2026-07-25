# HANDOFF — Agent Hub

> 이건 요약이 아니다. **다음 에이전트(어느 하네스든)를 위한 복구 기록**이다.
> 실행 확정판은 [`EXECUTION-PLAN.md`](./EXECUTION-PLAN.md)(단계 R0~R6)다 — 이 파일 최상단 재기획 블록을
> 읽은 뒤 그것부터 통독하라. 설계 근거·전 단계 스펙은 [`BUILD-SPEC.md`](./BUILD-SPEC.md)(특히 §0.0)에 있다.

- **원래 목표**
  여러 AI 코딩 에이전트(Claude Code · Codex/ChatGPT · Antigravity CLI · Grok · Cursor)를 한 사람이 쓸 때
  **작업 플로우가 끊기지 않고(핸드오프) 모델 성향과 무관하게 일관적으로 작동하는** 개인용 시스템을 이 레포에 구축한다.
  정본 원칙: 모든 상태는 Git에 커밋되는 파일에 산다. 도구는 소모품이다.

- **현재 단계**

  **[2026-07-25 Agent Hub 1.4.3 Gemini 3.6 실제 실행 복구 — 이 블록이 최신]**

  - **원래 목표**: 동적 catalog에 표시되는 `gemini-3.6-flash-{high,medium,low}`를 Agent Hub의
    공통 provider 경로와 연결 관리 GUI에서 실제로 실행하고, 단순 로그인·목록 조회와 실제 생성 성공을
    구분해 사용자가 연결 상태를 정확히 판단하도록 고칩니다.
  - **현재 단계**: Gemini의 public model ID 실행과 GUI의 bounded 실제 연결 테스트를 구현하고
    Agent Hub 1.4.3으로 검증·설치했습니다. Python editable package, Codex plugin, Claude Code user
    plugin이 모두 1.4.3이며, 새 MCP stdio process도 1.4.3과 `agent_hub_*` 도구 37개를 반환합니다.
  - **완료**:
    - Antigravity `fetchAvailableModels` 응답의 dictionary key를 실행 가능한 public model ID로
      유지합니다. 중첩 `internal_id`는 catalog metadata로만 노출하고 generation body의 model로
      치환하지 않습니다. non-stream, stream, 401 뒤 project/token refresh도 같은 public ID를
      유지합니다.
    - Gemini 3.6 Flash tier의 작은 출력 한도를 최소 256 token으로 보정하고 `-medium` suffix를
      `thinking_level=medium`으로 해석합니다.
    - GUI의 Gemini `연결 테스트`는 현재 선택 모델을 시작 시점에 고정해 짧은 실제 요청 한 번만
      보냅니다. 30초 timeout, 최대 512 output token, retry 0, tools·grounding·policy off로 제한하며
      소량의 사용량이 발생한다는 문구를 UI, README, provider-connect skill에 표시합니다. 상태 새로고침과
      catalog 조회는 계속 생성 요청을 보내지 않습니다.
    - 테스트 시작 전후의 auth generation을 비교하고 manager 종료를 terminal state로 보호해,
      재로그인·동의 변경·종료와 겹친 늦은 성공을 완료로 기록하지 않습니다. prompt, model output,
      provider 원문 오류는 job·GUI에 저장하거나 복사하지 않습니다.
    - 로컬 브라우저에서 `gemini-3.6-flash-high`를 선택한 뒤 실제 연결 테스트가
      `Gemini 선택 모델의 실제 응답까지 정상입니다.`로 완료되는 것을 확인했고 console error는
      0건이었습니다.
  - **미완**:
    - 현재 실행 중인 Codex Desktop과 Claude Code process는 설치 전 plugin/MCP를 적재했을 수 있어
      1.4.3을 확실히 사용하려면 각 host를 재시작해야 합니다.
    - live catalog와 실제 응답 가능 여부는 Google 서비스와 현재 계정 권한에 따라 이후 달라질 수
      있습니다. GUI 연결 테스트는 사용자가 누를 때마다 작은 생성 요청 한 번을 사용합니다.
  - **변경 파일**:
    - 실행·GUI: `src/google_antigravity_codex/{antigravity_api.py,chat.py}`,
      `src/agent_hub/connect_service.py`, `src/agent_hub/connect_ui/app.js`
    - 버전·plugin: `pyproject.toml`, `src/agent_hub/__init__.py`,
      `.claude-plugin/marketplace.json`,
      `hubs/{codex/.codex-plugin/plugin.json,claude-code/.claude-plugin/plugin.json}`
    - 문서·skill: `README.md`, `HANDOFF.md`, `hubs/{shared,codex,claude-code}/skills/provider-connect/SKILL.md`
    - 회귀 테스트: `tests/agent_hub/{test_connect_app.py,test_connect_service.py,test_hub_plugins.py}`,
      `tests/google_antigravity_codex/{test_agy_auth_provider.py,test_chat_grounding_image.py,test_stream_api.py}`
  - **검증 실행 결과**:
    - 새 회귀 테스트의 수정 전 실패 5건을 재현한 뒤 관련 suite → `135 passed`
    - credential 경로를 임시 디렉터리로 격리하고 `PYTHONPATH=.`을 지정한 전체 pytest →
      `684 passed, 2 skipped`; 첫 실행은 repo root import 경로가 없어 `scripts` collection error가
      났으며 코드 실패가 아니어서 같은 격리 조건에 import 경로를 추가해 재실행했습니다.
    - `./.venv/bin/ruff check .`, `node --check src/agent_hub/connect_ui/app.js`,
      `git diff --check` → 통과
    - `./scripts/check-hub-plugins.sh`, `./scripts/check-sync.sh`, `./scripts/test-phase1.sh`,
      `scripts/check_release_version.py --tag v1.4.3` → 통과
    - `./.venv/bin/python -m build` → 1.4.3 sdist·wheel 성공; wheel에서 Gemini adapter와 연결 UI
      파일 포함 확인
    - 설치 후 `agent-hub-setup --check --json` → changed 0;
      `agent-hub-doctor --json` → `8 pass, 0 warn, 0 fail`
    - Python package, Codex plugin, Claude Code user plugin → 모두 1.4.3 installed/enabled;
      새 MCP stdio → server 1.4.3, 도구 37개, 모두 `agent_hub_*`
    - source·Codex cache·Claude cache의 provider-connect skill 5개 SHA-256 일치
  - **현재 리스크**: live catalog의 nested `internal_id`는 불투명 metadata이므로 실행 ID로 다시
    사용하면 같은 404 회귀가 발생합니다. 이 대화에서 이미 실행 중인 MCP process는 설치된 1.4.3으로
    자동 hot reload되지 않습니다.
  - **Do-Not-Repeat**: catalog의 nested placeholder를 generation body의 model로 보내지 마세요.
    `agent_hub_status`나 model-list 성공만으로 실제 generation까지 성공했다고 쓰지 마세요. 연결 테스트
    결과에 prompt, model output, provider response body·원문 오류를 복사하지 마세요. 재로그인과 겹친
    테스트 결과를 auth generation fence 없이 완료 처리하지 마세요.
  - **다음 한 걸음**: Codex Desktop을 완전히 재시작한 뒤 새로 연 연결 관리 화면의 Gemini 카드에서
    `연결 테스트`를 한 번 실행해 host 재적재 후에도 1.4.3 실제 응답 성공 문구가 표시되는지 확인하세요.

  ---

  **[2026-07-24 Agent Hub 1.4.2 provider 연결·웹 모델 설정 전수 수정 — 이전 기록]**

  원래 목표는 Claude, Grok, Gemini, GPT의 동의·로그인·다시 로그인 버튼을 실제로 동작하게 고치고,
  같은 웹 화면에서 provider별 기본 LLM 모델까지 안전하게 선택하도록 만드는 것입니다. 공식 로그인
  소유권은 유지하고, 계정 변경은 사용자가 보는 로컬 GUI에서만 일어나게 했습니다.

  완료한 내용:

  - `agent-hub-connect`와 `python -m agent_hub.connect_app`가 `127.0.0.1` 전용 연결 관리 화면을 엽니다.
    네 provider의 동의, 로그인·다시 로그인, 연결 테스트, 연결 해제, 로컬 자격 증명 삭제를 한 화면에서
    처리합니다. Claude와 GPT는 각각 Claude Code와 공식 Codex 로그인을 실행하고, Agent Hub가 공동
    세션을 삭제하지 않습니다. Grok device code와 Google PKCE는 Agent Hub가 소유한 로컬 흐름입니다.
  - 브라우저 fragment의 난수 session을 `sessionStorage`로 옮긴 뒤 `X-Agent-Hub-Session` header로만
    전송합니다. 상태 변경 요청은 exact Origin, session, `X-Agent-Hub-Intent`, 화면에 표시한 provider별
    confirmation을 모두 확인합니다. cookie나 OAuth token을 브라우저 응답에 넣지 않습니다.
  - provider별 **Agent Hub 기본 텍스트 모델** 선택기를 추가했습니다. 로그인 전에는 설치된 안전 목록,
    준비된 연결에서는 live 목록을 사용하며, 목록 revision과 TTL을 확인한 정확한 text model ID만
    저장합니다. 환경변수로 관리되는 값, 목록에서 사라진 값, image model, 만료된 목록은 fail closed합니다.
    초기화는 모델 값만 지우고 다른 provider 설정은 보존합니다.
  - 저장 모델은 fixed/delegate/adaptive 실행 경로에 공통으로 주입됩니다. 호출자가 직접 지정한 모델이
    최우선이고, Gemini는 saved chat 모델·환경변수·활성 profile의 우선순위를 한 규칙으로 맞췄습니다.
    adaptive 실행은 시작 시점의 네 provider 모델을 모두 저장해 이어서 실행할 때 설정 변경의 영향을 받지
    않습니다. partial compare model map의 빈 자리도 보존합니다. Claude 계열 모델에는 profile이 넣은
    Gemini 전용 `thinking_level`을 전달하지 않습니다.
  - workflow가 자동으로 선택한 `reasoning_effort`는 최종 모델의 실제 adapter capability를 확인합니다.
    미지원 모델에서는 provider·model이 포함된 경고와 함께 자동값만 생략하고, 사용자가 직접 지정한 값은
    기존처럼 fail closed합니다. fixed recipe의 stage 값, adaptive write/review/release, participant별
    compare까지 같은 기준을 사용합니다.
  - non-Gemini 설정과 Gemini model preference는 process lock과 atomic replace로 저장합니다. 중첩 값의
    타입·허용 field·범위를 엄격히 검사해 손상된 설정을 덮어쓰거나 list/dict를 모델 문자열로 바꾸지
    않습니다.
  - Grok·Google pending OAuth에 `flow_id`, format version, 시작 시점 consent revision을 저장합니다.
    완료·취소·manager 종료는 자신이 시작한 flow만 조건부로 처리하고, token commit 직전에 같은 pending,
    같은 consent revision, 현재 consent를 provider별 cross-process auth lock 아래에서 다시 검사합니다.
    다른 창의 새 flow나 revoke/regrant 경계를 지난 응답은 token을 저장하지 않습니다.
    manager 종료의 cancel event와 token commit도 같은 manager lock으로 선형화해 종료가 먼저 이긴
    로그인은 credential을 저장할 수 없습니다. 기본 Claude·Codex 로그인 subprocess는 process group
    단위로 추적해 종료 시 bounded SIGTERM/SIGKILL로 정리합니다.
  - refresh 시작 시 token 파일 revision도 함께 기록합니다. 사용자가 GUI에서 로컬 로그인 정보를 삭제하거나
    다른 로그인이 token을 교체하면 진행 중이던 Google·Grok refresh는 새 token을 저장하지 않습니다.
    Google `whoami`는 refresh와 token 파일 보정을 하지 않는 읽기 전용 호출로 고정했습니다.
  - GUI 로그인 시작은 손상·구버전·만료·동의 revision 불일치 pending만 lock 안에서 자동 정리하고, 현재
    version의 정상 active flow는 보존합니다. token commit 뒤 pending 정리나 Google client 사본 저장만
    실패하면 로그인 성공을 유지하고 안정된 warning을 반환하며 manager가 소유권을 보존해 다시 정리합니다.
  - Google callback은 정확한 path와 OAuth state, 내부 flow 소유권을 확인한 single-winner job만 처리합니다.
    port 51121이 사용 중이면 기존 listener를 종료하지 않고 redirect URL/code 붙여넣기로 전환합니다.
  - 통합 `agent_hub_auth_*`와 별도 Grok·Google leaf MCP의 login/complete/refresh/logout은 자격 증명을
    직접 바꾸지 않고 `agent-hub-connect`만 안내하는 read-only 도구입니다. 네 통합 auth 도구는 실제
    로그인을 시작한 것처럼 성공을 반환하지 않고 모두 `provider_gui_required`를 사용합니다. GUI가 없는
    환경의 실제 변경은 사용자가 provider별 CLI script를 직접 실행해야 합니다.
  - status·job·logout 응답에서 token, verifier, credential 절대경로, account email, provider 원문 오류를
    제거했습니다. 로그인 URL은 지정한 Google/xAI HTTPS host와 기본 443 port만 허용합니다.
  - provider별 로그인 시작·테스트 시작·삭제 동작을 하나의 상태 머신으로 묶어, 시작 중 중복 click과
    consent 철회·credential 삭제가 서로 앞지르지 못하게 했습니다. UI도 provider가 busy일 때 login,
    test, delete, model control을 함께 비활성화합니다.
    로컬 credential 삭제 dialog는 전역 in-flight guard와 요청 시점 provider를 고정해 이중 제출,
    Escape 뒤 다른 provider 삭제, 실패 뒤 job/poll 관측 손실을 막습니다.
  - 모델 catalog는 provider 인증 세대에 묶었습니다. 동의 변경, 로그인 시작·종료, 연결 해제, 로컬
    credential 삭제 때 서버와 UI가 해당 catalog와 진행 중인 응답을 즉시 폐기합니다. 인증 변경과 겹친
    모델 목록 요청은 이전 계정의 revision을 되살리지 못하며 새 목록을 요구합니다.
  - 연결된 provider의 첫 모델 조회부터 live catalog를 사용하도록 고쳤습니다. GPT는 공식 Codex
    app-server에서 8개 모델, Gemini는 Antigravity에서 23개 모델을 읽었고
    `gemini-3.6-flash-{high,medium,low}`가 모두 표시되는 것을 로컬 브라우저에서 확인했습니다.
    Gemini의 로그인 전 fallback에도 같은 세 tier를 추가했습니다. Gemini live catalog의 public ID와
    내부 실행 ID가 다른 경우 project-scoped cache로 매핑하고, 새 process의 첫 generation에서도
    catalog를 다시 읽어 내부 ID를 사용합니다.
    GPT live catalog에 표시명이 같은 두 Luna 항목은 model ID를 함께 표시해 구분합니다.
  - consent version은 정확한 정수 현재 버전만 허용합니다. 손상됐거나 문자열·boolean·float로 저장된
    version은 미동의로 처리하고 재동의 시 원자적으로 복구하며, 이미 유효한 동의는 grant ID와 revision을
    유지합니다.
  - 이전 managed TOML block 안의 사용자 top-level 설정을 보존한 채 MCP block만 갱신하도록
    `agent-hub-setup` migration을 보강했습니다. 실제 설정 동기화에서 `approval_policy`와
    `sandbox_mode`를 block 밖으로 옮겨 보존했습니다.
  - Codex·Claude Code·공용 hub에 같은 `provider-connect` 스킬을 추가했고, README의 첫 경로를 GUI로
    바꿨습니다. 새 외부 runtime dependency는 추가하지 않았습니다.
  - release version을 1.4.2로 맞추고 Python editable package, Codex plugin cache, Claude Code user
    plugin을 업데이트했습니다. 설치된 MCP의 stdio initialize와 `tools/list`도 직접 확인했습니다.

  변경 파일:

  - 연결 GUI·서버: `src/agent_hub/connect_app.py`, `src/agent_hub/connect_service.py`,
    `src/agent_hub/connect_ui/{index.html,styles.css,app.js}`
  - 인증·동의: `src/agent_hub/core/{auth_state.py,consent.py}`,
    `src/google_antigravity_codex/{account.py,agy_auth.py,consent_cli.py,mcp_server.py,oauth_login.py,security.py}`,
    `src/grok_codex/{mcp_server.py,oauth_login.py,security.py}`, `src/openai_codex/auth.py`
  - 모델 설정·실행: `src/agent_hub/{capabilities.py,operations.py,orchestrator.py,provider_settings.py}`,
    `src/google_antigravity_codex/{chat.py,model_prefs.py,models.py}`, `src/openai_codex/models.py`,
    `src/orchestrate_codex/{recipes.py,runner.py}`
  - 설치·버전: `src/agent_hub/{__init__.py,local_setup.py}`, `pyproject.toml`,
    `.claude-plugin/marketplace.json`,
    `hubs/{codex/.codex-plugin/plugin.json,claude-code/.claude-plugin/plugin.json}`
  - 스킬·문서: `hubs/shared/skills/provider-connect/SKILL.md`,
    `hubs/codex/skills/provider-connect/SKILL.md`,
    `hubs/claude-code/skills/provider-connect/SKILL.md`, `README.md`, `HANDOFF.md`
  - 회귀 테스트: `tests/agent_hub/{test_connect_app.py,test_connect_service.py,test_hub_plugins.py,
    test_provider_expansion.py,test_adaptive_orchestration.py,test_server.py,test_setup.py}`,
    `tests/google_antigravity_codex/{test_agy_auth_provider.py,test_mcp_server.py,test_model_prefs.py,
    test_new_features.py,test_oauth_login.py,test_security_network_models.py}`,
    `tests/grok_codex/test_core.py`, `tests/openai_codex/test_core.py`,
    `tests/orchestrate_codex/test_core.py`

  검증:

  - credential 경로를 임시 디렉터리로 격리한 `./.venv/bin/python -m pytest -q` →
    `675 passed, 2 skipped`
  - `./.venv/bin/python -m ruff check .` → 통과
  - `node --check src/agent_hub/connect_ui/app.js` → 통과
  - `./scripts/check-hub-plugins.sh` → 통과
  - `./scripts/check-sync.sh` → 통과
  - `git diff --check` → 통과
  - `./.venv/bin/python -m orchestrate_codex.document_quality README.md`와 `HANDOFF.md` → 통과
  - `agent_hub_verify(user_facing=true)` → `README.md`와 최신 HANDOFF 블록 모두 경고 0건
  - `./scripts/test-phase1.sh` → disposable fixture acceptance 통과
  - release version 5개 필드 → 모두 `1.4.2`
  - `./.venv/bin/python -m build` → 1.4.2 sdist·wheel 생성 성공
  - wheel 확인 → `src/agent_hub/connect_ui/index.html`, `src/agent_hub/connect_ui/styles.css`,
    `src/agent_hub/connect_ui/app.js`의 배포 사본과
    `agent-hub-connect = agent_hub.connect_app:main` 포함
  - `./.venv/bin/agent-hub-connect --help` → 통과
  - 설치된 MCP stdio initialize와 `tools/list` → 37개, 모두 `agent_hub_*`
  - Codex·Claude Code plugin list → 1.4.2 installed, enabled
  - `agent-hub-setup --check --json` → changed 0
  - `agent-hub-doctor --json` → `8 pass, 0 warn, 0 fail`

  미완과 현재 위험:

  - 실제 provider 로그인·다시 로그인·동의 변경·모델 저장·generation은 계정 상태나 외부 사용량을
    바꾸므로 실행하지 않았습니다. 최종 read-only status에서 Claude·Grok·GPT는 ready이고 Gemini는
    consent가 있지만 로그인을 다시 완료해야 합니다.
  - Codex Desktop과 Claude Code의 실행 중 process는 설치 전 플러그인을 적재했을 수 있습니다. 설치된
    1.4.2 plugin과 MCP를 새 process에서 확실히 사용하려면 앱 또는 세션을 재시작해야 합니다.

  Do-Not-Repeat:

  - consent grant, OAuth code 교환, refresh, logout을 MCP tool 안에서 직접 실행하지 마세요.
  - GUI 실행 안내에 bare `agent-hub-connect`나 `python`이 PATH에 있다고 가정하지 말고 MCP가 반환한
    absolute `next_action.command`와 분리된 `next_action.args`를 사용하세요.
  - manager cleanup에서 provider 이름만 보고 pending을 지우지 말고 반드시 시작한 `flow_id`를 비교하세요.
  - token을 저장하기 전에 현재 pending과 consent revision을 auth-state lock 아래에서 다시 확인하세요.
  - 네트워크 refresh 뒤 token을 저장할 때는 시작 시 token 파일 revision도 비교하세요. GUI 삭제를 단순히
    같은 lock으로 직렬화하는 것만으로는 삭제 뒤 stale refresh가 token을 되살리는 일을 막지 못합니다.
  - Claude/GPT 공동 로그인을 Agent Hub의 연결 해제로 삭제하지 마세요.
  - Google port 51121의 기존 listener를 종료하지 말고 paste fallback을 유지하세요.
  - managed TOML block을 통째로 삭제해 block 안에 잘못 들어간 사용자 top-level 설정을 잃지 마세요.
  - 연결된 provider의 첫 모델 버튼을 정적 catalog 전용으로 되돌리거나 GPT·Gemini live ID를 고정
    allowlist로 제한하지 마세요.

  **다음 한 걸음:** Codex Desktop을 재시작한 뒤 `http://127.0.0.1:8765/`의 Gemini 카드에서
  `로그인`으로 Google PKCE를 완료하고 `최신 모델 목록 새로고침`의 출처가 live인지와
  `gemini-3.6-flash-high`, `-medium`, `-low`가 표시되는지 확인하세요.

  ---

  **[2026-07-23 run state·저장소 조사 경계 보강 — 이전 기록]**

  사용자 요청에 따라 현재 구현을 다시 조사했고, 우선순위가 높은 보안 경계 문제를 수정했습니다.

  완료한 내용:

  - run ID를 서버가 생성하는 12자리 소문자 16진수로 제한했습니다. canonical·legacy schema, 메모리 조회,
    파일 저장, resource URI는 같은 validator를 사용합니다.
  - 상태 디렉터리와 파일 접근을 private permission(`0700`/`0600`)으로 고정했습니다. 사용자 제어 symlink는
    거부하고, macOS의 `/var → /private/var`처럼 안전한 system alias만 허용했습니다.
  - 검증한 directory FD를 파일 작업이 끝날 때까지 유지하고 `open`·`replace`·`unlink`를 `dir_fd`
    기준으로 실행해 directory 교체와 path traversal이 외부 JSON 읽기·쓰기로 이어지지 않게 했습니다.
  - `inspect_codebase`와 모든 durable fact pack은 Git tracked + non-ignored untracked 파일만 읽습니다.
    Google durable writer의 중복 구현도 같은 canonical collector로 통합했고, Git에서 제외한 README나
    pyproject를 별도 경로로 다시 읽는 우회를 막았습니다. `src/` layout의 실제 package도 fact pack에
    포함해 문서 검증이 확인된 package 이름을 오탐하지 않게 했습니다.
  - filesystem root, home과 그 상위, 넓은 system/temp root, 민감 파일, symlink, workspace 밖 대상,
    1 MiB 초과 파일을 제외합니다. `secrets.yaml`, `.credentials.py`, `secrets.backup.txt` 같은 민감
    설정 이름 변형도 차단했습니다.
  - 저장소 읽기도 root directory FD에 고정하고 각 경로 구성요소를 no-follow 방식으로 열어, 검사 직후
    상위 디렉터리가 symlink로 교체돼도 workspace 밖 내용을 읽지 않습니다. manifest와 Git 명령 자체도
    해당 FD에서 실행하므로 root를 잠시 교체했다가 복원하는 ABA 우회가 외부 metadata를 노출하지 않습니다.
  - Git과 non-Git 파일 열거, 후보 수, 경로 bytes, 총 읽기 bytes, Git 상태 출력, 최종 context 길이에
    상한을 적용했습니다. Git file stream에는 deadline을 두고, manifest 부모 디렉터리 목록도 같은 문자
    예산을 공유합니다. 하위 디렉터리 조사에서는 Git status/log/diff도 같은 범위로 제한합니다. 제한 여부와
    생략 사유는 inspection metadata와 모델 입력에 함께 남깁니다.
  - run ID 없는 fixed full-state handoff는 기존 호환성을 위해 비영속 방식으로 유지했습니다. adaptive state는
    valid run ID가 필수이고, state와 별도 run ID를 함께 전달하면 중앙 runner가 일치 여부를 검사합니다.
  - 개발 환경에 `pip install -e '.[dev]'`를 다시 실행해 pytest, Ruff, build 도구와 editable package를
    설치했습니다.

  변경 파일:

  - `src/orchestrate_codex/store.py`
  - `src/orchestrate_codex/runner.py`
  - `src/orchestrate_codex/mcp_server.py`
  - `src/orchestrate_codex/gather.py`
  - `src/agent_hub/operations.py`
  - `src/agent_hub/core/repository_facts.py`
  - [`src/google_antigravity_codex/doc_facts.py`](./src/google_antigravity_codex/doc_facts.py)
  - `tests/orchestrate_codex/test_core.py`
  - `tests/agent_hub/test_adaptive_orchestration.py`
  - `tests/agent_hub/test_server.py`
  - `tests/google_antigravity_codex/test_writing_release.py`
  - `HANDOFF.md`

  검증:

  - `./.venv/bin/python -m pytest -q` → `324 passed, 11 skipped`
  - `./.venv/bin/python -m ruff check .` → 통과
  - `git diff --check` → 통과
  - `./scripts/check-sync.sh` → 통과
  - `./.venv/bin/python -m build` → sdist·wheel 생성 성공
  - `./.venv/bin/python -m orchestrate_codex.document_quality HANDOFF.md` → 통과
  - 최신 블록 `agent_hub_verify(user_facing=true)` → 경고 0건

  남은 위험과 범위:

  - content-only focus symbol이 focus scan 상한 뒤에 있으면 자동 선택되지 않을 수 있다. 이 경우
    `focus_scan_truncated`가 `true`로 반환되므로 더 구체적인 파일 경로로 다시 조사해야 한다.
  - adaptive compare의 실제 provider 호출 수 계산과 downstream 답변 전달, prompt/resource 소유권,
    concurrent continue의 CAS/lock은 이번 보안 변경 범위에 포함하지 않았습니다.
  - 작업 시작 기준은 `d87562c`였고, 이 블록과 보안 변경은 같은 로컬 커밋으로 기록합니다. 원격 push는
    요청받지 않아 수행하지 않습니다.

  **다음 한 걸음:** adaptive `compare` participant 수를 최대 호출 수 계산에 반영하고 각 provider의 실제 답변을
  downstream context에 전달하는 실패 회귀 테스트부터 추가하세요.

  ---

  **[2026-07-18 README 사람 문체 전면 개편 — 이 블록이 최신]**

  `README.md`를 실제 사용자가 읽는 순서로 다시 썼다. 여러 모델을 오갈 때 겪는 불편과 제작 이유를 첫
  문단에서 설명하고, 설치 → 모델 로그인 → Codex·Claude Code 연결을 바로 따라 할 수 있게 앞에 배치했다.
  공개 도구, adaptive 실행, 문서 검사와 내부 안전장치는 필요한 사용자가 뒤에서 읽도록 옮겼다. 추상적인
  장점 나열과 대칭적인 불릿을 줄이고 명령 주변의 설명도 짧고 구체적으로 고쳤다.

  adaptive plan은 `fc9dcf1a...`, run ID는 `9b67815a45f9`다. deep/high 저장소 조사는 58개 파일을 읽고
  성공했다. 문체 검토 단계는 잘못 고른 `review_diff`가 clean worktree에서 `empty_diff`를 반환해 무효였고,
  마지막 작성 단계는 약 282초 뒤 시간 예산을 소진했다. 모델이 만든 완성본은 없었으며, 실패한 결과를
  README에 사용하지 않았다.

  현재 README는 로컬 자연스러운 한국어 검사와 `agent_hub_verify` checker v3에서 경고 0건이다. 관련
  테스트는 `23 passed`, `git diff --check`도 통과했다. 렌더링 화면 검수는 앱 브라우저의 로컬 파일 접근
  제한 때문에 하지 못했다.

  원격 `main`에는 이전 변경 커밋 `cca2a74`까지 푸시돼 있다. 이번 README와 기록 문서 변경은 아직
  커밋하지 않았다.

  **다음 한 걸음:** README diff를 사람이 한 번 더 읽고 만족하면 세 문서를 커밋한다. 별도 제품 수정으로는
  planner가 기존 파일 검토에 `review_diff`를 고르지 않도록 capability 계약을 보강할 수 있다.

  ---

  **[2026-07-18 README 전면 재작성·resumable workflow 검증 — 이전 기록]**

  `README.md`를 현재 구현을 기준으로 처음부터 다시 썼다. 처음 보는 사용자가 제품의 역할부터 설치,
  provider 로그인, Codex·Claude Code 연결, 공개 도구 26개, 고정·adaptive workflow, timeout과 재개,
  Consistency Gate, handoff·memory, 보안, 검증까지 순서대로 따라갈 수 있는 구조다. 공개 schema와 manifest,
  테스트를 대조해 workflow ID, 기본값, 설정 경로, 상태 디렉터리와 환경 변수도 실제 구현에 맞췄다.

  작성 자체도 Agent Hub의 긴 실행 경로로 검증했다. live status는 provider `3/3 ready`였고, Opus 4.8
  planner가 deep/high 조사 → Gemini deep/high 검증 → Opus 작성의 3단계 DAG를 만들었다. plan SHA는
  `39bb66effcb7bf80f0ecf62a70fcc4e45d39ed5c386f204775bad3dc79b467ad`, run ID는
  `350b7c2057e4`다. 첫 Opus 조사가 provider timeout으로 멈췄지만 같은 state에서 Sonnet 5로 바꿔 재개했고,
  이후 두 step까지 완료했다. 최종 checker v3 경고는 0건이다.

  이 실험에서 숨김 경로 정규화가 `.gemini/settings.json`의 첫 점을 지우던 문제와, 조회한 공개 state를
  `continue`에 넘겼을 때 `Run loaded.` 문구가 pause 응답에 남던 문제를 추가로 고쳤다. 둘 다 특정 README나
  저장소 이름에 의존하지 않는 공통 수정이며 기존 회귀 테스트에 사례를 넣었다.

  버전은 `1.3.2`다. Ruff, pytest `291 passed, 11 skipped`, Hub plugin·Ruler sync, Phase 1 fixture,
  doctor, README 품질 검사, 릴리스 버전 동기화, sdist·wheel build와 `git diff --check`를 통과했다. 현재
  소스를 직접 부른 README verify도 checker v3 경고 0건이다. 앱 브라우저는 로컬 `file://` 접근을 보안
  정책으로 차단해 렌더링 화면 검수만 수행하지 못했다.

  아직 커밋·푸시하거나 설치 캐시를 갱신하지 않았다. 실행 중인 MCP 프로세스도 마지막 숨김 경로·pause 문구
  수정보다 먼저 적재됐으므로 최신 동작 확인에는 앱 재시작이 필요하다.

  **다음 한 걸음:** 전체 diff를 검토해 커밋한 뒤 Codex와 Claude Code 플러그인을 1.3.2로 다시 적재하고,
  새 작업에서 숨김 `source_file` verify와 timeout 후 `continue` 응답을 한 번씩 live 확인한다.

  ---

  **[2026-07-17 로컬 LLM 오케스트레이션 연구 문서 재구성 — 이전 기록]**

  Gemma 4 E4B와 Qwen 3.5 9B 로컬 정책 모델 연구를
  `LOCAL-ORCHESTRATION-RESEARCH.md`에 처음부터 다시 정리했다. 현재 정본에는 예전 학습 트리가 없으므로
  `/Users/naen/Git/agent-hub-legacy-chanwoo-20260717`을 읽기 전용 근거로 사용했다. 연구 구현 기준은
  `9d37b59`, 종료 문서 커밋은 `90510db`다. 보존 저장소는 수정하지 않았고 새 provider outcome을 수집하거나
  Frontier-v4 sealed holdout 64개를 열지 않았다.

  새 문서는 초기 Gemma JSON 계약 파일럿, 이중언어 8-model outcome, Frontier-v1/v2/v3/v4, Qwen LoRA
  feasibility와 prompt-only coordinator까지 전체 연대기를 담는다. 수치와 해석을 분리하고 평가 방법,
  타당성 한계, 중단 결정, 재사용 자산과 재개 조건을 함께 기록했다. README에는 현재 비활성 연구라는 짧은
  안내와 문서 링크만 추가했다.

  Agent Hub 실행 기록:
  - live status: Claude/Grok/Gemini `3/3 ready`.
  - 첫 adaptive plan은 조사 루트가 현재 정본이라 보존 자료를 놓칠 위험이 있어 실행하지 않았다.
  - 보존 저장소를 루트로 둔 첫 재계획은 잘린 JSON으로 validator가 거부했다.
  - 최종 plan: Gemini 3.5 Flash High, deep/high 조사 → 초안 → 3-provider claim review → 최종 합성,
    plan SHA `6ce95ce3b3862f3c8f88098017b3422b70cd52c2b27b12792444fecc3ee2adc6`, 최대 9 calls.
  - end-to-end run은 MCP 전체 300초 상한에서 timeout되어 산출물을 사용하지 않았다.
  - `agent_hub_write`: Claude Opus 4.8 high, 품질 재작성 1회 뒤 checker v2 통과. 이 초안의 잘못된
    “Frontier-v1 수치 없음” 주장과 빠진 초기 단계를 로컬 원자료로 교정했다.
  - 최종 `agent_hub_verify(doc_class=durable,user_facing=true)`: 경고 0건.

  검증: 연구 문서와 README의 `orchestrate_codex.document_quality` 통과, README pytest 3 passed,
  Ruler sync 통과, Markdown render 통과, `git diff --check` clean. 변경 파일은
  `LOCAL-ORCHESTRATION-RESEARCH.md`, `README.md`, `HANDOFF.md`다.

  **다음 한 걸음:** Claude Code를 재시작한 새 작업에서 `document-write` 스킬이 자동 선택되는지 한 번 확인한다.

  ---

  **[2026-07-17 문서 문체 규칙 강제 — 이전 기록]**

  README 문체 규칙이 다른 세션에서 빠지는 원인을 확인했다. 그 세션은 최신 `agent-hub-mono`가 아니라
  원격이 `Chanwoo-act/agent-hub`인 예전 `/Users/naen/Git/agent-hub` 체크아웃에서 작업했다. 이 체크아웃은
  `/Users/naen/Git/agent-hub-legacy-chanwoo-20260717`과 같은 이름의 `.bundle`로 보존했고, 원래 경로는
  `agent-hub-mono`를 가리키는 symlink로 바꿨다. 최신 저장소의
  한국어 규칙은 예전 체크아웃의 `AGENTS.md`에 없었고, MCP 직접 쓰기 경로도 문체 검사를 자동 실행하지 않았다.

  `agent_hub_write`는 이제 로컬 문서 품질 검사를 반드시 거친다. 실패하면 기본 한 번 전체 문서를 다시 쓰고,
  그래도 통과하지 못하면 `success=false`와 `document_quality_failed`를 반환한다. 응답의 `quality_gate`에는
  검사 적용·통과 여부, 검사기 버전, 재작성 횟수와 정책 출처가 남는다. `agent_hub_verify`도 내부 실패를 MCP
  성공으로 감싸지 않는다. 고정 workflow는 `korean_style`과 잘림 경고를 자동 재작성 사유로 사용하며,
  재작성 횟수를 다 쓴 뒤에는 run을 실패로 끝낸다.

  README 기본 프로필은 자연스러운 존댓말로 바꿨고, 독백체와 설명 없는 내부 용어를 확인하는 검사를 추가했다.
  Codex와 Claude Code에는 같은 `document-write` 스킬을 배포한다. 예전 README는 새 검사에서 실패했고 현재
  Meapri README는 통과했다. Ruff, pytest `276 passed, 11 skipped`, Ruler·Hub skill sync, Phase 1 fixture,
  README 검사와 diff-check가 모두 통과했다. 버전은 `1.3.1`이다.

  Meapri 커밋 `d864888`을 `origin/main`에 푸시했다. Codex 플러그인은 1.3.1로 다시 설치했고 Claude Code도
  1.2.0에서 1.3.1로 갱신했다. 두 설치 캐시에 `document-write`가 있으며 Claude의 component inventory에도
  표시된다. 새 MCP 프로세스의 직접 호출에서는 `quality_rewrite_attempts=1`, 실패 응답의 `isError=true`,
  `success=false`, `checker_version=2`를 확인했다.

  **다음 한 걸음:** Codex와 Claude Code를 재시작하고 새 작업에서 `document-write` 스킬이 자동으로 선택되는지
  확인한다.

  ---

  **[2026-07-17 코드 조사·능동 추론·자연스러운 문서 작성 통합 — 이전 기록]**

  Codex 재시작 뒤 1.3 live workflow를 다시 돌렸다. Opus 4.8 planner가 deep/high 조사, Claude·Gemini 병렬
  평가, 세 provider Consistency Gate, Claude 최종 정리의 5단계 DAG를 만들었고 4 wave로 완료했다. 세 모델은
  `partially_supported`에 3/3 합의했다. 이 과정에서 기존 deep 수집기가 파일마다 앞 5천 자만 보내
  `gather.py` 뒤쪽 구현을 놓치는 문제가 확인됐다.

  수집기는 이제 저장소를 넓게 확인하는 1단계와 핵심 파일을 깊게 읽는 2단계로 동작한다. 조사 요청에 나온
  파일명·함수명·기능으로 핵심 파일을 다시 고르고, 작은 파일은 전문을, 큰 파일은 관련 구간 여러 곳을 읽는다.
  모든 조각에는 원본 줄 번호와 `complete`·`partial` 상태가 붙으며 결과에도 전문 파일·부분 파일·줄 범위를
  남긴다. 같은 저장소 재현에서 후보 250개 중 57개를 확인했고 `gather.py`, `document_quality.py`, `verify.py`
  등 핵심 파일 4개는 전문이 들어갔다.

  첫 live status에서 만료된 Gemini 토큰 때문에 `2/3 ready`가 나온 문제도 고쳤다. `probe=true`는 저장된
  갱신 토큰으로 인증을 갱신한 뒤 같은 status 응답에서 새 상태를 읽는다. 전체 자동 검사는 Ruff, pytest
  `270 passed, 11 skipped`, Ruler·Hub skill sync, Phase 1 fixture, 문서 검사를 통과했다. 현재 MCP 프로세스는
  수정 전 코드를 이미 적재했으므로 새 수집기의 live 재검증에는 앱 재시작이 한 번 더 필요하다.

  adaptive planner가 로컬 저장소 조사에 provider 웹 검색을 잘못 쓰던 빈틈을 막았다. 새
  `inspect_codebase` capability는 `shallow`·`standard`·`deep` 범위에 따라 실제 코드, 공개 스키마, 설정,
  테스트, 생성 문서 동기화와 Git 상태를 모은다. plan의 각 LLM 단계에는 `reasoning_effort=low|medium|high`가
  들어가며 Claude Opus 4.8, Grok 4.5, Gemini 3.x의 실제 요청 필드로 전달된다. 지원하지 않는 모델은
  명시적 오류로 끝난다.

  README와 장기 문서 작성 규칙은 `instructions/.ruler/30-documents.md`에 추가했다. 생성된 `AGENTS.md`와
  `CLAUDE.md`, Codex·Claude Code의 공통 adaptive 스킬까지 동기화했다. 번역투와 작업 중계 문장은
  `src/orchestrate_codex/document_quality.py`에서 검사하며 README 테스트와 local verify가 같은 규칙을 쓴다.

  실제 dogfood에서 Opus 4.8 planner가 deep/high 코드 조사 뒤 high 작성 단계로 이어지는 2단계 DAG를
  만들었다. 후보 250개 중 30개 파일을 읽고 2 wave·2 leaf call로 완료했다. Grok 4.5의 Responses
  `reasoning.effort=high`와 Gemini 3.1 Pro High의 `thinking_level=high`도 짧은 실호출로 확인했다. 자동 검사는
  Ruff, pytest `265 passed, 11 skipped`, Ruler sync, Hub plugin sync, README 문체 검사, diff-check까지 통과했다.

  Agent Hub 버전은 `1.3.0`으로 올렸다. 자세한 실행 근거는 [`RUN-REPORT.md`](./RUN-REPORT.md) 맨 위 1.3
  항목에 있다. 새 코드를 적재한 앱 재시작 뒤 live workflow 재검증이 남아 있다.

  **다음 한 걸음:** Codex를 재시작하고 같은 deep workflow를 다시 실행한다.

  ---

  **[2026-07-17 Adaptive orchestration·Consistency Gate·앱 플러그인 — 이전 기록]**

  원래 목표였던 모델 간 일관성을 두 층으로 나눠 완성했다. Ruler는 동일한 프로젝트 규칙을 Codex와 Claude
  Code에 배포하고, Agent Hub는 실제 provider 호출 때 정본 정책을 다시 주입해 `policy_sha256`과
  `request_sha256`을 남긴다. 닫힌 선택 문제는 `agent_hub_compare_models.consistency`로 엄격한 JSON 응답을
  받아 합의율·응답 수·provenance를 검사한다. 불일치나 provider 실패는 성공으로 포장하지 않고 사람 검토로
  돌린다.

  `workflow_id=adaptive`는 고정된 Claude→Grok→Gemini 순서를 사용하지 않는다. planner LLM이 단계, provider,
  의존 관계, fallback과 마지막 결과 단계를 고른다. 로컬 validator가 capability/provider allowlist, cycle,
  orphan, 단일 final sink, 단계 수와 최대 호출 횟수를 검사하고, scheduler가 시작할 수 있는 단계를 동시에 실행한다.
  검토된 plan을 다시 넘기면 planner 호출 없이 validate-only 경로로 실행한다. 실패한 단계는 선언된 fallback을
  사용하고 모두 실패하면 의존 단계를 막은 채 fail-closed로 끝낸다.

  Codex와 Claude Code 플러그인은 같은 엔진을 호출하는 얇은 cockpit으로 정리했다. 공통
  `adaptive-orchestrate` 스킬은 `hubs/shared/skills/`에서 두 플러그인으로 동기화하며, Claude Code에는
  `/agent-hub-plan`, `/agent-hub-run` 명령도 추가했다. provider별 MCP나 별도 orchestration 로직은 플러그인에
  복제하지 않는다. 버전은 `1.2.0`, 공개 도구는 26개 그대로이며 workflow만 5개로 늘었다.

  실제 dogfood에서는 Gemini 3.5 Flash High가 독립 코드 리뷰 2개와 최종 종합 단계의 DAG를 만들었다. 첫
  wave의 두 리뷰가 병렬로 실행됐고, Claude 리뷰가 완료 계약을 지키지 않자 Grok fallback으로 전환한 뒤 두
  결과가 준비된 후 Gemini 종합이 실행됐다. 이 검토에서 확인된 untracked 파일 위험은 일반 diff 리뷰에서
  기본 제외하고, adaptive 리뷰만 파일 수·크기·바이너리 제한을 둬 명시적으로 포함하도록 수정했다.

  ---

  **[2026-07-17 Claude·Grok capability 확장 — 이전 기록]**

  통합 공개 도구는 26개로 유지하면서 Gemini에만 연결돼 있던 직접 작업을 provider 중립 구조로 바꿨다.
  `agent_hub_search`, `agent_hub_write`, `agent_hub_review_diff`, `agent_hub_release_draft`는 Claude·Grok·Gemini를
  선택할 수 있고, `agent_hub_compare_models`는 세 provider를 같은 입력으로 비교한다. `agent_hub_generate_image`는
  Grok과 Gemini를 지원하며 `agent_hub_release_snapshot`은 순수 로컬 작업으로 분리했다.

  `agent_hub_chat.images[]`와 `workspace_root`를 공통 입력으로 추가했다. 로컬 이미지 경로는 명시한 root 안에서만
  읽고, 크기·MIME·민감 경로·원격 URL을 검사한 뒤 provider 형식으로 변환한다. 실제 첨부 이미지 smoke에서
  Claude Sonnet 5와 Grok 4.5가 모두 `지원 범위`를 정확히 읽었다. Claude web search와 Grok web search도 현재
  subscription OAuth에서 citation을 포함해 성공했고, Claude·Grok 비교도 두 모델 모두 정상 완료했다.

  기본 모델은 live catalog에 맞춰 Claude Sonnet 5와 Grok 4.5로 갱신했다. Claude 5 계열이 더 이상 받지 않는
  `temperature`는 adapter가 제거하고 `temperature_ignored_by_model` warning을 남긴다. Agent Hub 버전은
  `1.1.0`, Claude·Grok 내부 adapter 버전은 `0.3.0`이다.

  자동 테스트와 전체 검증 결과는 루트 [`RUN-REPORT.md`](./RUN-REPORT.md)에 기록한다. Grok 이미지 생성은
  endpoint·응답·로컬 캐시까지 mock으로 확인했지만 실제 호출은 비용이 발생하므로 이 작업에서는 실행하지 않았다.

  ---

  **[2026-07-16 출력 토큰·잘림 판정 수정 — 이전 기록]**
  ACT-5400 장문 작성 중 Gemini 3.5 Flash High가 4,096 출력 토큰 중 대부분을 high-thinking에 사용하고
  실제 본문 164토큰만 낸 뒤 `finish_reason=max_tokens`로 끝난 사례를 재현했다. 기존 Antigravity writing
  wrapper와 broker가 종료 이유를 잃어 356자짜리 미완성 문서를 성공으로 처리한 것이 원인이었다.
  Claude/Gemini/Grok 및 conductor의 chat/write 기본 예산을 65,536으로 통일하고 Gemini schema 상한을
  131,072로 확대했다. 32,768 재시험은 본문 2,137자에서 다시 한도 종료됐지만 실패로 정확히 잡혔고,
  65,536 재시험은 2,868자와 종료 표식까지 생성해 `finish_reason=stop`으로 완주했다. Claude `max_tokens`,
  Gemini `max_tokens`/`length`, Grok `length`/`incomplete` 종료는
  이제 `success=false`와 `incomplete_finish_reason:*`로 전달되어 broker가 부분 문서를 완료본으로 채택하지 않는다.
  관련 회귀 테스트를 추가했고 compileall, 전체 pytest 204개, Ruler sync, Phase 1 fixture, diff-check가 통과했다.

  ---

  **[2026-07-16 실행 완료 — 이전 완료 기록]**
  재기획 후 `EXECUTION-PLAN.md` R1~R6를 실행하고 **완성 정의 3개 증거를 전부 커밋**했다:
  - **E1** custody (`3527245`): `model-access/leaves.manifest.json` + `model-access/evidence/`(실제 orchestrate run).
  - **E3** 크로스-하네스 메모리 (`4ccddb8`): Claude Code(`claude -p`)와 Codex(`codex exec`)가 같은
    basic-memory store의 노트를 읽음.
  - **E2** 크로스-허브 핸드오프 왕복 (`f997a34`): 패킷만으로 Claude↔Codex 양방향 인계(doctor.sh `[5/5]` 구현 / R4 문서 수정).
  - 부수: R4 메모리 배선(`d8e35ec`), R5 핸드오프 템플릿(`4025201`), R6 hubs 재배선·pal/orca 은퇴(`f1e9848`).
  검증: `doctor.sh` OK(5/5), `check-sync.sh`·`test-phase1.sh` PASS, `claude plugin validate` 통과,
  `git diff --check` clean, 잔여 `.ruler` 없음.
  **남은 것은 사용자 게이트뿐**: R3(claude-codex 구독 plan-lane 실과금 확인) + 선택적 R6 leaf live load
  (antigravity `list_models`, OAuth 필요). R0의 orchestrate-codex push는 이미 완료(HEAD==origin `e694888`).

  ---

  **[2026-07-16 재검토·재기획 — 배경, 위 실행의 근거]**
  6개 프로젝트(agent-hub 본체 + gemma router + 외부 published 플러그인 4종) 재검토 결과, 계획을 현실에 맞춰
  갱신했다. 상세는 `BUILD-SPEC.md` §0.0. 요지 셋:
  1. **모델접근·오케스트레이션은 이미 작동 중.** `~/.orchestrate_codex/runs/`에 claude-codex + grok-codex +
     antigravity leaf가 협업해 완료한 run이 있다(recipe `deep_readme`, `status=completed`, verify 통과). 콕핏은
     Codex GUI, 지휘자는 orchestrate-codex다. **단 이 코드·설정이 전부 git 밖**(orchestrate-codex 작동 버전
     v0.4/v0.5는 uncommitted, published는 v0.2; 배선은 off-git `~/.orchestrate_codex/leaves.json`; leaf는
     `github.com/Meapri/*` 외부 레포). agent-hub는 이들을 전혀 참조하지 않는다 → **custody가 최우선 리스크.**
  2. **연속성 코어(Phase 2 핸드오프 / Phase 3 메모리)는 여전히 미구축** — 프로젝트의 실제 목적인데 비어 있다.
  3. **로컬 모델 라우팅(gemma/qwen)은 반증되어 종료.** 독립 split에서 전 구성 promotion gate `passed=False`
     (최선 primary 51.9% vs 정적 baseline 55.2%, regret 3.63 vs 1.94). uncommitted Qwen coordinator도
     orchestrate-codex 중복이라 **추진 안 함.** 관련 dirty tree는 정리/아카이브 대상.

  **새 우선순위** — 실행 절차는 `EXECUTION-PLAN.md`의 R번호를 따른다:
  **R0**(🔒 사용자: orchestrate-codex push, R3 콘솔 확인, leaf OAuth) → **R1** custody(leaf 매니페스트 +
  run 증거 커밋) → ~~R2 문서 정리~~(✅ 완료) → **R3** claude-codex 과금/ToS 확인(🔒) → **R4** Phase 3
  메모리 배선 → **R5** Phase 2 크로스-허브 핸드오프 실연 → **R6** hubs/ 재배선 + dual-hub 확정.
  (문서 동기화·라우터 정리는 2026-07-16 세션들에서 완료: `90510db` + 재기획 문서 커밋.)

  ---

  **[2026-07-15 기록 — 이하 라우터 개발 로그는 반증된 경로이며 역사 기록으로만 보존]**
  Phase 1(지시 일관성) 완료. **아키텍처 확정(2026-07-15): Option 1 — Claude Code + Codex 듀얼-허브,
  콕핏은 orca(MIT)를 포크 없이 얹고, 자체개발은 git 기반(substrate)에만.** BUILD-SPEC은 5-Phase
  (1 지시 ✅ / 2 핸드오프 / 3 메모리 / 4 모델 접근 / 5 허브 패키징).
  **지금: Phase 5의 허브 플러그인 2종(Codex + Claude Code) 스캐폴드 완료.** Claude Code 플러그인은
  `claude plugin validate` 통과. 두 플러그인 모두 **실기 설치·MCP 실기동은 미완**(도구 설치 + API 키 필요).
  **Phase 4 보조 경로로 Gemma 4 E4B 영어 orchestration router의 로컬 MLX QLoRA pilot도 완료**했으며,
  이어서 **Orca 8-arm 영어/한국어 outcome 수집·blind judge·MLX 재학습 파이프라인까지 구현·실행**했다.
  새 adapter는 bilingual schema/capability를 학습했지만 holdout의 primary-arm/exact-route 일반화는 실패했다.
  이어서 **공개 평가 + 비-holdout 로컬 outcome을 쓰는 fail-closed prior**를 구현하고, 운영 profile을
  Sol / Opus 4.8 / Gemini 3.1 Pro / Grok 4.5의 **4-provider `frontier-v1`**으로 축소했다.
  public prior는 primary를 바꾸지 않으며, contract상 필요한 독립 슬롯을 보존하고 충돌/누락을 보정하며
  외부 검증 route의 불필요한 provider reviewer는 제거한다. 마지막으로
  **Qwen 3.5 9B 4-bit MLX 전환을 실제 검증했지만 학습 feasibility gate에서 중단**했다.
  이후 사용자 결정으로 **Gemma E4B 데이터 재구축 경로를 다시 열어 frontier-v2 sealed 평가까지 완주했다.**
  140개 source task를 작성하고 108개 development/recovery task의 4-arm 후보·blind judge outcome을 수집했다.
  balanced 160-step adapter는 contract/capability는 학습했지만 provider primary validation은 22.2%에 그쳤다.
  대신 Gemma capability + train-only empirical primary policy + 독립-slot prior를 결합한 hybrid가 validation
  59.7%를 기록해 policy manifest와 80개 label-free holdout prediction을 먼저 freeze했다. 그 뒤 32-task
  holdout을 수집한 sealed 결과는 accepted 71 route에서 primary 40.8%(majority 32.4%), exact 33.8%였지만
  한국어 primary 27.5%·reasoning 0%라 **후보 profile은 승격하지 않았고 활성 기본 learned router는 여전히 없다.**
  이어서 기존 family와 겹치지 않는 **한국어 code/reasoning frontier-v3 80 family**를 만들고 64 development를
  실제 4-provider outcome으로 수집했다. capability-local balanced Gemma E4B의 최선 210-step checkpoint와
  validation-only calibration은 validation primary 61.5%였지만, 먼저 봉인한 새 16-task holdout에서는
  primary 41.7%로 static GPT baseline과 동률이고 regret은 6.583 대 6.509로 더 나빴다. reasoning primary/exact도
  25.0%/8.3%라 **v3 역시 승격하지 않았고 기존 runtime은 그대로다.** 이후 router가 비가시 정책 모델이라는
  사용자 결정을 반영해 **frontier-v4 개발 경로를 영어-only 입력/JSON 출력으로 전환**했다. train-only 영어
  scorecard+retrieval, thinking-off, prompt-aligned 160-step QLoRA, 영어 empirical policy, fail-closed slice gate를
  결합한 후보가 열린 영어 validation 37 route에서 schema 100%, primary 51.4%, exact 45.9%, regret 1.658로
  static GPT 40.5%/4.649를 이겼다. 이어서 8 capability별 40개씩 **영어-only 독립 family 320개**를 고정해
  development 256(train 192/valid 64) + label-free sealed holdout 64와 manifest를 만들었다. 사람 검토 상태는
  2026-07-16 사용자 승인으로 전환했고 review packet/task hash를 별도 attestation에 묶었다. 이어서 provider
  self-judge exclusion, judge-centered median, quality/latency utility, scenario-cluster bootstrap CI를 수집 정책으로
  고정했다. 승인된 development 256 task의 후보·심사 각 1,024 cell 수집도 완료했고, capability-aware verifier와
  robust label gate로 247 task를 채택해 train 463/valid 154 route를 만들었다. observed/capability-capped
  prompt-aligned Gemma E4B QLoRA의 7개 checkpoint를 validation했지만 최선도 primary 51.9%/regret 3.633으로
  static GPT 55.2%/1.945를 못 넘어 모두 bootstrap promotion gate에서 탈락했다. holdout 64는 계속 봉인했다.
  현재는 이미 받은 **Qwen 3.5 9B 4-bit를 파인튜닝 없이 prompt-only multi-turn coordinator로 전환**했다.
  strict action schema + fail-closed state machine + Orca runner를 구현했고, 실제 1-call writing과 GPT thinker →
  Claude worker → Gemini verifier의 3-call code trajectory를 완주했다. 다만 개발 smoke 2건뿐이라 active runtime은
  여전히 없다. 사용자 결정에 따라 **로컬 coordinator 연구는 여기서 종료·보존**한다. Prompt-only라면 이미 쓰는
  cloud model에 deterministic state machine을 붙이는 편이 별도 local 9B runtime보다 현재 조건에서 실용적이라는
  결론이며, 전체 비용 우위 주장은 하지 않는다. 최종 근거와 재개 조건은 하위 `RESEARCH-CLOSURE.md`에 고정했고
  frontier-v4 holdout 64는 미개봉 상태로 남겼다.

- **완료**
  - Phase 1 (커밋 `e885454`/`1f4c290`/`08ee9d7`/`e76d5a6`): 단일 정본 → Ruler 배포.
    산출물 `instructions/.ruler/*` + `ruler.toml`(Ruler `0.3.44` 고정), 생성물 `CLAUDE.md`/`AGENTS.md`/`.gemini/settings.json`,
    스크립트 `sync.sh`/`check-sync.sh`/`test-phase1.sh`, `README.md`.
  - **Phase 5 — Codex 플러그인** `hubs/codex/`: `.codex-plugin/plugin.json` + `.mcp.json`(bare object; memory, pal)
    + skills(`handoff`/`takeover`/`route-to`) + README. JSON 유효.
  - **Phase 5 — Claude Code 플러그인** `hubs/claude-code/`: `.claude-plugin/plugin.json` + `.mcp.json`
    (**`mcpServers` 키로 감쌈** — Codex와 형식 다름) + skills(`handoff`/`takeover`/`route-to`) + README.
    JSON 유효 + `claude plugin validate ./hubs/claude-code` → **✔ Validation passed**.
  - 두 플러그인 공통: memory=`uvx basic-memory mcp`, pal=`uvx --from git+…/pal-mcp-server`, 키는 `${VAR}` 참조만(값 미포함).
  - **Phase 4 — 로컬 policy router pilot** `model-access/policy/gemma4-e4b-router/`: 고정 revision의
    `mlx-community/gemma-4-e4b-it-4bit` + MLX-VLM QLoRA. 영어 synthetic contract 데이터 70/10/20,
    80-step adapter, test에서 schema-valid 100% / target 90% / exact route 70%. 실행·근거는 하위 `RUN-REPORT.md`.
  - **Phase 4 — bilingual outcome smoke**: Orca로 8개 provider arm(OpenAI 3 / Claude 2 /
    Antigravity Gemini 2 / Grok 1)을 실행하고, 동일 task 답변 익명화 → 8-way blind judge(자기 점수 제외) →
    outcome route 생성 → 40/80-step MLX QLoRA 완료. Google은 Gemini CLI가 아니라 `agy` 사용.
    미학습 holdout 24 route에서 80-step adapter는 schema/capability 100%지만 primary 8.3%, exact 0%.
  - **Phase 4 — source-backed hybrid prior**: 8개 arm의 2026-07-15 공식/독립/커뮤니티 근거를
    `data/public_model_priors.json`에 출처별로 분리하고, 원본 48행에서 투영한 frontier historical 42행을
    16 scenario로 dedupe한 뒤 frontier 4 arm을 `configs/model-priors-frontier-v1.json`으로 결정적으로 컴파일.
    holdout/balanced 입력 경로 차단, legacy judge는 scenario당 0.25 관측치, public은 최대 2
    pseudo-observation으로 제한. sensitivity 분석과 batch latency는 진단 전용이며 primary override는
    명시적으로 off. unpaired EN/KO local language update도 제외.
  - **Phase 4 — frontier-v1 operational warm-start**: 기존 8-arm 자산은 보존하고, 서로 다른 provider의
    Sol / Opus 4.8 / Gemini 3.1 Pro / Grok 4.5만 쓰는 runtime profile·schema·prompt·prior·projection·adapter 추가.
    예산은 모델 tier가 아니라 최대 provider call 수(`low=1`, `medium=2`, `high=3`)이며
    `low + cross_model`은 입력에서 거부. 기존 48 route를 네 arm score로 재계산해 불가능한 6행을 뺀
    42행 historical warm-start(34/3/5)를 만들고 80-step MLX QLoRA 완료.
  - **Phase 4 — final Qwen 3.5 feasibility decision**: 고정 revision
    `mlx-community/Qwen3.5-9B-MLX-4bit@938d8919…` 5.6 GB 다운로드·검증과 20-route base 평가 완료
    (JSON 100%, schema 5%, exact 0%). 정상 LoRA는 `CustomKernel` VJP 부재로 첫 step 전에 실패했고,
    fused rotary 8개를 끈 2-step probe도 3분 이상 progress report/adapter를 만들지 못하며 swap 사용이 약 0.88 GB 증가해 중단.
    20/80-step은 gate off, Qwen adapter 없음. 상세는 하위 `QWEN35-ATTEMPT.md`.
  - **Phase 4 — Gemma frontier-v2 data-path restart**: 기존 batch 단위 수집을 `(task, arm)` 단위로 바꾸고
    task latency, raw/normalized usage, provider가 실제 보고한 cost만 저장. deterministic constraint verifier와
    top-two winner-margin gate가 실패 label을 차단하며, 매 task 저장 및 `--resume`을 지원. 실제 4-provider Orca
    pilot 2 task의 후보 8회+blind judge 8회가 전부 성공했고 두 label 모두 verifier 100%, margin 4.0으로 통과.
    6 route를 넣은 Gemma E4B 8-step QLoRA smoke도 완료(19.44M/0.245%, peak 10.531 GB). 같은 train 6 route의
    schema 0→50%, exact 0→16.7%지만 한국어 3 route는 schema-invalid라 adapter 승격 금지.
  - **Phase 4 — Gemma frontier-v2 full sealed run**: 8 capability × EN/KO × standard/advanced의 base 128 task와
    code-recovery 12 task를 생성하고 scenario-family split leakage 0을 확인. development 96 + recovery 12를
    실제 수집해 verifier/margin/latency-tie gate로 source task 86개, train 145 + valid 72 route를 채택했다.
    unbalanced 120-step과 provider-balanced 160-step QLoRA를 모두 실행했지만 standalone primary validation은
    각각 18.1%/22.2%. train-only capability winner policy를 결합하자 validation primary 59.7%, exact 47.2%,
    provider independence 100%로 개선. manifest SHA `026d966f77bb90a270746766e426697e72b88ab863aeb75cce741c97b6c9dcef`와
    label-free 80 route를 먼저 봉인한 후에만 32-task holdout의 256 result cell을 수집했다. accepted 71 route
    sealed score는 raw→hybrid primary 31.0→40.8%, regret 4.089→3.404, exact 33.8%. majority 32.4%는 넘었지만
    EN primary/exact 58.1% 대 KO primary 27.5%/exact 15.0%, reasoning primary 0%라 runtime 승격은 보류.
  - **Phase 4 — Gemma frontier-v3 Korean sealed run**: frontier-v2와 겹치지 않는 한국어 80 family(code 20,
    reasoning 60)를 development 64/holdout 16으로 먼저 고정. development 후보·blind judge 각 256 cell을
    수집해 51/64 task, 111 route를 gate 통과시켰다. v2 train-only data와 합친 뒤 global balance 240-step과
    capability-local balance 280-step을 모두 학습했고, 후자의 210-step checkpoint가 validation schema 100%,
    raw primary 38.5%로 최선. validation confusion calibration은 primary 61.5%, exact 42.3%였지만 static-GPT보다
    regret이 이미 나빴다. manifest `63cfa865f1d3dde19530af57bc376b7e3da73ccd7aaa611bf14afc98d2869eea`와
    label-free 36 route/seal을 먼저 고정한 뒤 holdout 후보·judge 각 64 cell을 수집. 16/16 task가 gate를 통과한
    sealed score는 primary 41.7%, exact 30.6%, regret 6.583으로 static-GPT primary 41.7%/regret 6.509를 못 넘었다.
    code primary/exact 75.0%, reasoning primary 25.0%/exact 8.3%라 v3 candidate도 승격 금지.
    postmortem으로 schema 통과뿐 아니라 predeclared static baseline 대비 primary와 mean regret을 모두 엄격히
    개선해야 통과하는 `scripts/check_promotion_gate.py`를 추가했고, v3 validation은 regret 6.705 대 5.641로
    의도대로 exit 1 처리된다.
  - **Phase 4 — frontier-v4 영어-only development**: router를 사용자 비가시 정책으로 한정해 한국어 생성 목표를
    제거했다. 영어 train 72 route/28 scenario에서 scorecard와 lexical retrieval context를 만들고 self-retrieval을
    차단했다. compact prompt는 최대 987 tokens이며 train/inference 모두 thinking-off다. prompt-aligned QLoRA는
    160-step/peak MLX 16.813 GB로 완료했고, 영어 train-only primary policy 및 deterministic constraint repair와
    결합했다. validation 37 route에서 schema/capability 100%, primary 51.4%, exact 45.9%, fail-closed regret 1.658;
    static GPT 40.5%/4.649 대비 overall 및 eligible slice gate를 통과했다. 실사용 feedback은 raw task 대신 SHA-256만
    저장하는 local ignored JSONL 경로를 추가했다. 결과는 sealed 증거가 아니며 활성 runtime은 바꾸지 않았다.
  - **Phase 4 — frontier-v4 독립 task bank**: code/reasoning/research/writing/summarization/translation/planning/
    operations 각 40개의 영어 routing family를 수작업 synthetic specification으로 작성했다. split은 capability마다
    train 24/valid 8/test 8, 전체 standard/advanced 160/160이다. 기존 v2/v3 task/family ID overlap은 0이고 prompt
    token-set Jaccard 최대치는 bank 내부 0.8125/과거 bank 대비 0.82다. 최초 감사에서 0.953인 v2 요약 재사용 1건을
    찾아 교체했다. 사용자의 명시적 승인과 review packet/task SHA-256을 별도 review record에 고정했고, task가
    바뀌면 승인이 유지되지 않는다. collector는 승인 record와 SHA-256 일치를 강제한다.
  - **Phase 4 — frontier-v4 수집/평가 hardening**: 동일 provider judge를 candidate 평가에서 모두 제외하고,
    judge별 median centering 후 독립 3-provider score의 median을 쓰는 robust aggregation을 추가했다. 2점 quality
    tie 안에서만 quality 0.85/latency 0.15 utility를 사용한다. 과거 v3 raw 64-task dry run은 winner 6개 변경,
    accepted 51→53, utility tie-break 9건, candidate당 independent provider 최소 3을 확인했다. 과거 per-task USD는
    Claude만 보고해 cost coverage 0/64이므로 cost weight는 0으로 고정하고 missing을 0원으로 간주하지 않는다.
    promotion에는 scenario 단위 cluster bootstrap CI를 추가했고 v4는 primary/regret improvement 95% CI 하한>0을 요구한다.
  - **Phase 4 — frontier-v4 development raw matrix + robust labels**: 승인/hash gate를 통과한 영어 256 task에 대해
    4-provider candidate 1,024 cell과 blind judge 1,024 cell을 Orca resume-safe 방식으로 전부 수집했다. Grok의
    incomplete structured envelope 2건은 저장 후 재시도했고 수동 답변 보정 없이 완료했다. frozen code schema의
    required/properties 모순을 provenance-visible legacy repair로 교정하고, code/research/summarization/translation은
    objective hard verifier, reasoning/writing/planning/operations은 diagnostic verifier로 고정했다. robust gate는
    247/256 task를 채택하고 margin<3인 9개를 제외해 train 463 route/185 scenario, valid 154/62를 만들었다.
    dataset manifest가 raw/result/code/policy/output hash와 128 schema repair 적용을 묶으며 sealed test는 0이다.
  - **Phase 4 — frontier-v4 Gemma independent validation**: train-only policy/context를 463 train route에서만
    컴파일하고 observed 617-route/capped 658-route prompt dataset을 만들었다. unbalanced 80/160/240/320과 capped
    80/160/240 checkpoint는 모두 final schema 100%였지만 static GPT primary 55.2%/regret 1.945보다 나빴고,
    primary/regret bootstrap CI와 slice gate를 전부 통과한 후보가 없었다. 최고 primary는 capped 240의
    51.9%였으나 regret 3.633이라 승격/동결/holdout 수집을 하지 않았다.
  - **Phase 4 — Qwen prompt-only multi-turn pilot**: `MODEL-QWEN35.lock`의 5.6 GB local model을 재사용해 role/model을
    분리한 action prompt, delegate/finish JSON schema, budget/dependency/independent-verifier/completion-feasibility
    state machine, coordinator-only bounded repair, Orca E2E runner를 추가했다. 8-capability first-action smoke는
    schema 8/8, 약 0.78–1.48초였다. 실제 writing 1-call은 Claude worker→finish, 실제 verified code 3-call은
    GPT thinker→Claude worker→Gemini explicit PASS verifier→finish로 성공했다. 이는 wiring evidence이며 품질 승격 근거는 아니다.
  - **Phase 4 — 로컬 coordinator 연구 종료 기록**: Antigravity Gemini 3.1 Pro High로 회고 초안을 만들고 로컬
    RUN-REPORT/Git 상태와 대조해 `RESEARCH-CLOSURE.md`로 교정했다. 사실/해석/결정을 분리하고 v2/v3/v4 수치,
    Qwen MLX failure, prompt-only smoke, 중단 경제성, 재사용 자산과 명시적 재개 조건을 한 문서에 고정했다.

- **미완** (실행 절차·수용 기준은 `EXECUTION-PLAN.md` R0~R6이 확정판)
  - **E1 custody** (`EXECUTION-PLAN.md` R1): leaf 매니페스트 + orchestrate run 증거를 agent-hub에 커밋.
    orchestrate-codex v0.5.1(`8ae14bf`)은 로컬 커밋만 있고 unpushed — push는 사용자 게이트(R0).
  - **claude-codex 과금·ToS 확인** (R3): 구독 plan-lane 실과금 여부 미검증. 판정 전 대량 사용 금지.
  - Phase 5 잔여 (R6): hubs/ 재배선 — `pal`은 **은퇴(§0.0)**, `.mcp.json`을 실제 leaf/conductor로 교체 후
    실기 로드 검증. (기존 절차 중 `uv tool install basic-memory`와 Claude Code `--plugin-dir` 로드 확인은 유효,
    pal 확인 부분만 폐기.)
  - Phase 2 (R5): 크로스-허브 리허설(Claude Code↔Codex 실제 작업 이관 왕복). 핸드오프 규약은 스킬에 인라인됨.
  - Phase 3 (R4): basic-memory 실기동 + `scripts/doctor.sh`. 데이터 디렉토리는 레포 `memory/data/`로 **결정 완료**
    (EXECUTION-PLAN §7).
  - ~~Phase 4: 게이트웨이(LiteLLM/OpenRouter)~~ — **은퇴(2026-07-16 재기획)**. 모델접근은 provider leaf
    플러그인으로 충족됐고, 워커 스폰은 orchestrate-codex broker가 담당(PAL 아님). 잔여는 위 E1/R3뿐.
  - Phase 4 router 후속은 **중단**: frontier-v3와 frontier-v4 Gemma single-shot 모두 static-GPT baseline을 못 넘겼고,
    prompt-only Qwen도 두 wiring smoke 외 이점을 증명하지 못했다. v2/v3 holdout은 이미 열렸고 v4 holdout 64는
    미개봉이므로 어떤 prompt/model 선택에도 사용하지 않는다. `RESEARCH-CLOSURE.md`의 재개 조건이 충족되고 사용자가
    명시적으로 연구를 다시 열기 전에는 새 trajectory collection, tuning, calibration, holdout scoring을 하지 않는다.
    structured code verifier는
    명시된 JSON contract/test vector를 검증하지만 arbitrary code를 실행하지 않고, research verifier도 제공된
    source ID만 검사하며 live web source를 확인하지 않는다. 이 경계를 유지하거나 별도 sandbox/source fetch
    verifier를 추가해야 한다.
    arbitrary code 실행과 live source 확인 경계는 그대로이므로 trajectory 평가에도 sandbox/source verifier가 따로 필요하다.
  - ~~orca: 설치(as-is) → Claude Code·Codex 로그인 재사용 확인~~ — **은퇴(2026-07-16 재기획)**. 콕핏은
    Codex GUI이고 지휘자는 orchestrate-codex다(BUILD-SPEC §0.0). orca는 설치하지 않는다.

- **변경 파일 (이번 세션)**
  - 신규: `hubs/codex/**`(6파일), `hubs/claude-code/**`(6파일 — `.claude-plugin/plugin.json`, `.mcp.json`,
    `skills/{handoff,takeover,route-to}/SKILL.md`, `README.md`).
  - 갱신: `BUILD-SPEC.md`(듀얼-허브 + orca 미포크 콕핏 + Phase 5 양 플러그인 반영, Do-Not-Repeat #11), `HANDOFF.md`.
  - 신규: `model-access/policy/gemma4-e4b-router/**`(모델 revision lock, 영어/bilingual seed data,
    sequence-aware completion QLoRA, 평가·실행 CLI, 테스트, `RUN-REPORT.md`). 모델 weight/adapter/report는 Git 제외.
  - 이번 갱신: 같은 디렉토리에 `configs/arms.toml`, route-v2 schema/prompt, 영어·한국어·blind-holdout task,
    Orca outcome collector, dataset build/merge/rebalance, generic schema evaluator, outcome tests와 문서 추가.
    생성된 model output report와 adapter는 Git 제외; route label dataset은 Git에 포함.
  - 이번 추가 갱신: `data/public_model_priors.json`, 생성 스냅샷 `configs/model-priors-frontier-v1.json`,
    `src/orchestration_router/priors.py`, `scripts/build_model_priors.py`, `scripts/rerank_route.py`,
    `scripts/route_provider.py`, `tests/test_priors.py`; `scripts/evaluate.py`, 하위 README/RUN-REPORT, 이 HANDOFF 갱신.
  - 이번 frontier-v1 갱신: `configs/{arms-frontier-v1.toml,frontier-v1-runtime.json,outcome-frontier-v1.toml,
    model-priors-frontier-v1.json}`, `schema/route-frontier-v1.schema.json`, `prompts/router-system-frontier-v1.txt`,
    `data/outcomes-frontier-v1-historical*/`, `scripts/{project_frontier_outcomes.py,build_policy_manifest.py}`,
    `scripts/build_outcome_dataset.py`의 `frontier-v1` policy 경로,
    `src/orchestration_router/evaluation.py`, 관련 테스트·문서. 기존 8-arm 파일/adapter는 덮어쓰지 않음.
  - 이번 Qwen 종료 갱신: `MODEL-QWEN35.lock`, `configs/{qwen35-frontier-v1-runtime.json,
    outcome-qwen35-frontier-v1-probe.toml}`, `QWEN35-ATTEMPT.md`; thinking-off 학습/평가 일치,
    model-lock 선택 다운로드/manifest, fused-rope training fallback, 명시적 runtime profile 요구를 코드에 반영.
  - 이번 Gemma 재개 갱신: `data/outcome_tasks_frontier_v2_pilot.jsonl`, `data/frontier-v2-pilot/**`,
    `configs/outcome-frontier-v2-pilot-smoke.toml`, `src/orchestration_router/verifiers.py`,
    `scripts/{collect_orca_outcomes.py,build_outcome_dataset.py}`, 관련 outcome/verifier/builder 테스트와 하위 문서.
  - 이번 frontier-v2 full 갱신: `data/outcome_tasks_frontier_v2_{development,holdout,code_recovery}.jsonl`,
    `data/frontier-v2-{development,code-recovery,training,training-balanced,holdout-labelled}/`, frozen prediction/seal,
    `configs/{outcome-frontier-v2.toml,outcome-frontier-v2-balanced.toml,outcome-policy-frontier-v2.json,
    frontier-v2-candidate-runtime.json,policy-manifest-frontier-v2.json}`, `src/orchestration_router/outcome_policy.py`,
    verifier/outcome 확장, `scripts/{build_frontier_v2_task_bank.py,build_outcome_policy.py,predict_task_bank.py,
    score_accepted_frozen_predictions.py,score_frozen_predictions.py}`, 관련 테스트와 README/RUN-REPORT.
  - 이번 frontier-v3 갱신: `data/outcome_tasks_frontier_v3_ko_{development,holdout}.jsonl`과 manifest,
    `data/frontier-v3-{ko-development,ko-holdout,training,training-balanced,training-cap-balanced}/`,
    `configs/{outcome-frontier-v3-ko-balanced.toml,outcome-frontier-v3-ko-cap-balanced.toml,
    outcome-policy-frontier-v3-ko.json,primary-calibration-frontier-v3-ko.json,
    frontier-v3-ko-calibrated-candidate-runtime.json,policy-manifest-frontier-v3-ko.json}`,
    `src/orchestration_router/primary_calibration.py`, `scripts/{build_frontier_v3_ko_task_bank.py,
    build_primary_calibration.py,check_promotion_gate.py}`, capability-local rebalance와 Grok thought-envelope recovery,
    관련 테스트·문서.
    Adapter와 raw collection/evaluation report는 Git 제외지만 local에 보존.
  - 이번 frontier-v4 개발 갱신: `FRONTIER-V4-PLAN.md`, 영어-only prompt context/outcome policy/runtime/config,
    `data/frontier-v4-en-prompt-training/`, `prompts/router-system-frontier-v4.txt`,
    `src/orchestration_router/prompt_context.py`, `scripts/{build_prompt_context.py,
    build_prompt_augmented_dataset.py,record_route_feedback.py}`, `schema/route-feedback-v1.schema.json` 및 테스트.
    `evaluate.py`는 thinking 토글·영어 slice·runtime constraint repair를 지원하고, promotion gate는 invalid route를
    worst-regret으로 계산하며 language/category/budget non-inferiority를 검사한다. adapter/report는 Git 제외로 보존.
  - 이번 frontier-v4 task-bank 갱신: `scripts/build_frontier_v4_en_task_bank.py`,
    `data/outcome_tasks_frontier_v4_en_{development,holdout}.jsonl`과 manifest,
    `tests/test_frontier_v4_en_task_bank.py`, `FRONTIER-V4-TASK-BANK-REVIEW.md`. collector에 human-review/hash gate와
    regression test를 추가했다.
  - 이번 frontier-v4 pre-collection 갱신: `scripts/approve_frontier_v4_task_bank.py`,
    `data/outcome_tasks_frontier_v4_en_review.json`, `configs/frontier-v4-en-collection-policy.json`; `outcomes.py`에
    robust judge aggregation/utility, `check_promotion_gate.py`에 scenario-cluster bootstrap을 추가하고 collector raw
    manifest가 관련 code/review SHA-256을 고정하도록 강화했다. 관련 outcome/promotion/approval 테스트 갱신.
  - 이번 frontier-v4 validation/Qwen pivot 갱신: independent outcome policy/context와 prompt dataset 2종,
    QLoRA config/evaluation ordering/manifest provenance hardening, `MODEL-GEMMA4-12B.lock`(metadata-only target),
    `QWEN-PROMPT-COORDINATOR.md`, `prompts/coordinator-system-qwen-v1.txt`,
    `schema/coordinator-action-v1.schema.json`, `src/orchestration_router/coordinator.py`,
    `scripts/{run_qwen_coordinator.py,run_qwen_orchestration.py}`, 관련 테스트와 README/RUN-REPORT/계획 갱신.
    Qwen/Orca trajectory report와 model/adapters는 Git ignored local artifact다.
  - 이번 연구 종료 갱신: Antigravity 초안을 fact-check해 `RESEARCH-CLOSURE.md`를 추가하고 하위 README,
    `QWEN-PROMPT-COORDINATOR.md`, `FRONTIER-V4-PLAN.md`, root `HANDOFF.md`를 archived 상태로 맞췄다.

- **검증 실행 결과**
  - Phase 1: `test-phase1.sh` / `check-sync.sh` 통과(이전 세션, 유효).
  - Codex 플러그인: `python3 -m json.tool` → plugin.json, .mcp.json JSON 유효. (codex CLI 미설치로 스키마 검증은 미실행.)
  - Claude Code 플러그인: JSON 유효 + `claude plugin validate ./hubs/claude-code` → `✔ Validation passed` (claude v2.1.204).
  - 실기 환경 점검(2026-07-15): `uv 0.11.28`/`uvx` 존재. `uvx basic-memory` → v0.22.1 설치·실행 확인, `mcp` 서브커맨드 존재
    → 두 플러그인의 memory 서버 명령 유효. API 키(GEMINI/GROK/OPENAI/OPENROUTER) **전부 unset** → PAL MCP 실기동 미검증.
    orca 설치법 `brew install --cask stablyai/orca/orca` 유효(공식 docs + brew가 tap 인식; 첫 실행 시 `~/.claude`·`~/.codex` 임포트 제안).
  - **아직 안 한 것**: 어느 플러그인도 실제 세션에 로드해 MCP 서버 기동·스킬 로드를 확인하지 않음. 실기동 차단 요인 = API 키 미설정 + 인터랙티브/GUI 로드(사용자 환경 작업).
  - Gemma router: 데이터 200개/영어 100개 schema·누수 검증, pytest 3개 통과, 8-step smoke와 80-step
    영어 QLoRA 완료(peak 8.72 GB), base↔adapter test 20개 비교, 실제 `scripts/route.py` conduct 경로 smoke 통과.
  - Bilingual outcome router(2026-07-15): 실제 8-arm 후보·judge 영어/한국어/holdout 전부 수집,
    dataset validation 통과(원본 48 route, balanced 133 total, holdout 24), pytest 6개 통과.
    40-step 및 80-step QLoRA 완료(19.44M trainable, peak 11.66 GB). Holdout base/raw/balanced 비교:
    schema 0/95.8/100%, capability 0/95.8/100%, primary 8.3/0/8.3%, exact 모두 0%.
  - Hybrid/frontier-v1: `pytest` **26 passed**, compileall 및 public/config JSON 유효, compiled snapshot
    재생성 일치 및 16-scenario dedupe/holdout 차단/local-language-off/call-cap/paired-metric 테스트 통과.
    80-step QLoRA는 19.44M trainable, peak 11.66 GB. 실제 한국어 `route_provider.py` smoke에서
    Sol primary / Opus secondary / Gemini 3.1 Pro reviewer를 보존하고 세 provider 분리 확인.
    둘 다 session 관측이며 frontier 전용 train/smoke log는 별도 보존하지 않음.
    최종 local policy manifest SHA `6b2a1d3eba6a9298b406b31e712ed888390018ee79dc958bea8759154805ccac`.
  - 같은 24-route holdout의 post-hoc 탐색 평가: aggressive prior는 primary label 8.3→20.8%였지만
    quality regret 2.452→3.571로 악화되어 폐기. 선택한 fail-closed policy는 primary/regret을 그대로
    유지하고 provider-diverse route 100%, `cross_model` reviewer 충족 100%. exact route는 여전히 0%.
  - frontier-v1 historical holdout projection 20 route(post-hoc): base schema 0%, 새 adapter schema/capability/
    decision/budget/verification 100%, primary/exact 20%. paired legacy-judge regret 6.657,
    within 1/2/5 points 20/45/60%. hybrid는 primary를 보존하고 expected multi-arm 9/9,
    expected cross-model reviewer 8/8 provider independence 충족. 새 sealed 성능 주장이 아님.
  - Qwen 3.5 final attempt(2026-07-15): HF 고정 revision 다운로드와 missing-file verify 통과. base 20-route 평가는
    23.73초, max RSS 5.62 GB, process swap 0. 첫 train은 21.639M trainable에서
    `Primitive::vjp Not implemented for CustomKernel`; checkpoint off도 동일. 비-fused rotary fallback은 오류를 넘었지만
    2-step이 3분 이상 progress report 없이 정체되고 system swap 사용이 약 0.88 GB 증가해 종료. adapter/20-step/80-step 없음.
  - Gemma frontier-v2 pilot(2026-07-15): `pytest` 34 passed, compileall/diff-check 통과. Orca 4-arm 후보·심사
    16회 전부 성공. code label=Opus 98.667, Korean writing label=Sol 86.333, 각 margin 4.0/verifier 100%.
    Claude CLI만 cost를 보고했으며 후보+심사 subtotal USD 0.344614(전체 provider 비용 아님).
    8-step QLoRA 13.57초/peak MLX 10.531 GB; 같은 6 train route base→adapter는 schema 0→50%, exact 0→16.7%.
  - Gemma frontier-v2 full(2026-07-15): base task 128(각 capability 16, EN/KO 64/64, standard/advanced 64/64),
    supplemental code 12. development completed matrix 768 cell + code recovery 96 cell. accepted train/valid 145/72.
    120-step 129.27초/peak MLX 11.662 GB, balanced 160-step 167.18초/peak MLX 11.649 GB. balanced validation
    schema 100%, capability 97.2%, primary 22.2%; hybrid validation primary 59.7%, exact 47.2%, regret 1.361.
    freeze 후 holdout candidate/judge 각 128 cell 완료(실패 성공 cell 재사용, Grok JSON-schema recovery).
    holdout 28/32 source accepted→71/80 route coverage 88.8%; sealed primary 40.8%, exact 33.8%, regret 3.404,
    raw primary 31.0%, majority 32.4%. EN 58.1%/KO 27.5%; 승격 금지. Claude-reported holdout subtotal
    USD 6.400327(다른 provider 비용 제외). 최종 `pytest` **49 passed**, 세 dataset validation, compileall,
    `check-sync.sh`, `test-phase1.sh`, `git diff --check` 통과.
  - Gemma frontier-v3(2026-07-16): 새 task bank 테스트 3개와 scenario-family 누수/이전 v2 ID 중복 0 확인.
    development 64 task의 후보·judge 512 cell 완료, gate 51/64 accepted→train/valid 85/26 route. v2 train-only와
    merge한 230/98 route 및 global-balanced 360/capability-balanced 415 train route 모두 validation 통과.
    QLoRA 두 번 완료(19.44M trainable, peak 약 12.0 GB); capability-local checkpoint validation 비교 후 step 210 선택.
    calibrated validation schema 100%, primary 61.5%, exact 42.3%. policy/prediction freeze 후 holdout 128 cell 완료,
    16/16 accepted→36 sealed route. 최종 primary 41.7%, exact 30.6%, regret 6.583; code 75.0%/75.0%, reasoning
    25.0%/8.3%. static-GPT primary 41.7%, regret 6.509라 승격 금지. 최종 `pytest` **59 passed**,
    v3 데이터셋 5종 validation, compileall, task/calibration/manifest 재생성 일치, `check-sync.sh`,
    `test-phase1.sh`, `git diff --check` 통과. 새 promotion gate 테스트 2개와 실제 v3 exit 1도 확인.
  - Gemma frontier-v4 development(2026-07-16): 영어-only prompt dataset 72/37/0, 모든 train sequence <=987
    tokens. 160-step QLoRA 완료(19.44M trainable, peak MLX 16.813 GB). 선택 profile validation은 schema/capability
    100%, primary 51.4%, exact 45.9%, fail-closed regret 1.658; static GPT primary 40.5%, regret 4.649.
    overall+eligible slice promotion gate exit 0. 전체 `pytest` **67 passed**, v4 dataset validation 109 examples,
    compileall, `check-sync.sh`, `test-phase1.sh`, `git diff --check` 통과.
  - Frontier-v4 task bank(2026-07-16): generator 결과 total 320/development 256/holdout 64, capability별 40,
    split 192/64/64, EN 320, difficulty 160/160, prior ID/family overlap 0, prompt similarity max 0.8125/0.82.
    전용 pytest 5개 및 전체 pytest **73 passed**, compileall, manifest 재생성, `git diff --check` 통과.
    pending manifest를 넣은 collector lock smoke도 Orca 상태 확인/출력 디렉토리 생성 전에 의도대로 실패했다.
    provider/Orca 호출은 0회.
  - Frontier-v4 pre-collection(2026-07-16): 사용자 approval record 생성 후 generator 재실행에도 동일 hash 승인 유지,
    전체 pytest **79 passed**. 과거 v3 development raw matrix robust dry run은 64 task 중 accepted 53(기존 51),
    winner change 6, median margin 7.25, utility tie-break 9, candidate당 independent provider 최소 3, comparable USD
    coverage 0 task. 이 dry run의 provider 호출은 0회.
  - Frontier-v4 development collection(2026-07-16): candidate/judge 각 1,024 cell 완전 행렬, task/arms/collector/
    outcome/verifier/review hash 일치. capability-aware robust dataset은 247/256 task accepted, 9 margin reject,
    train 463 route/185 scenario, valid 154/62, test 0. legacy code schema repair 128회와 collection/runtime verifier
    hash 차이를 dataset manifest에 명시. targeted pytest 17 passed.
  - Frontier-v4 Gemma validation + Qwen prompt pilot(2026-07-16): independent prompt dataset 617/658 route 모두
    schema validation 통과. Gemma 7 checkpoint는 schema 100%였으나 모두 static GPT primary/regret와 bootstrap gate
    탈락. Qwen first-action 8/8 schema, 실제 Orca writing 1-call과 verified code 3-call 완료. coordinator 테스트 10개를
    포함한 전체 pytest **95 passed**, compileall, 두 dataset validation, `scripts/check-sync.sh`,
    `scripts/test-phase1.sh`, `git diff --check` 통과. Orca runtime ready와 worktree comment도 현재 pivot으로 갱신.
  - Research closure(2026-07-16): Antigravity draft를 로컬 사실과 교차 확인한 `RESEARCH-CLOSURE.md` 262줄,
    상세 정본 4개 링크와 archived 상태 문서 일치 확인. 전체 pytest **95 passed**, compileall,
    `scripts/check-sync.sh`, `scripts/test-phase1.sh`, `git diff --check` 재통과.

- **현재 리스크**
  - `.mcp.json`은 API 키를 `${VAR}` 참조로만 담는다 — **실제 키를 커밋하지 말 것.**
  - Codex 플러그인 스키마(`.codex-plugin/plugin.json`/`.mcp.json`)는 2026-07 웹문서 2개 교차확인 기준, 실기 미검증 —
    설치 전 `codex app-server generate-json-schema`로 검증(플러그인 README 참조). (Claude Code 쪽은 validate 통과.)
  - basic-memory/PAL MCP 실행 명령은 각 도구 README(2026-07) 기준 — 버전 갱신 시 재확인.
  - basic-memory(0.22.1)는 `openai`/`litellm`/`tokenizers`를 의존성으로 끌어오고 vector embeddings(`reindex`)를 지원한다
    → 민감 노트 저장 전 임베딩을 로컬/off로 고정했는지 확인(Do-Not-Repeat #7).
  - Phase 1 리스크(Ruler bridge 등) 유효 — 정본 편집은 `instructions/.ruler/`만.
  - Gemma adapter는 Gemma license 적용 대상이며 공개 업로드는 하지 않았다. 현재 test는 masking 탐색 중 일부
    조회했으므로 exploratory 수치다. production 주장 전 outcome-labelled 새 holdout이 필요하다.
  - Outcome holdout은 처음엔 분리됐지만 이번 prior 개발에서 이미 조회했고 source task도 8개뿐이다.
    특히 label이 Grok에 12/24 몰렸고 exclude-self judge 편향도 있으므로 이후 수치는 전부 post-hoc
    exploratory다. 새 sealed holdout-v2 없이는 provider 우월성이나 production 자동 실행 근거로 사용하지 말 것.
  - Public prior의 숫자는 source-backed **ordinal heuristic**이지 객관 benchmark 점수가 아니다. API list
    price는 CLI/subscription 과금과 같다는 뜻이 아니며, community 링크는 숫자에 직접 반영하지 않았다.
  - frontier historical 16 scenario winner는 Opus 8 / Sol 7 / Grok 1 / Gemini 0이고 8/16이 top-two margin
    2점 이하다. 새 adapter도 historical holdout primary를 Opus/Sol로만 출력했으므로 실제 provider selector로
    신뢰하지 말 것. 4-arm 축소는 수집/계약을 단순화했을 뿐 데이터 부족을 해결하지 않는다.
  - Qwen learned-router profile은 실패 재현과 base 평가용 기록이지 운영 후보가 아니다. 새 prompt-only coordinator도
    실제 trajectory가 2건뿐인 development pilot이며 provider 우월성/production 품질 근거가 아니다.
    `route_provider.py`는 `--profile`을 요구하며 활성 기본 learned router가 없다. MLX-VLM에 differentiable Qwen 3.5
    fused path가 생기기 전에는 비-fused 20/80-step을 재시도하지 말 것.
  - frontier-v2 pilot adapter는 학습 예시 6개뿐이고 validation/test가 없다. train 재평가 수치를 일반화로
    인용하거나 `configs/frontier-v1-runtime.json`의 adapter로 바꾸지 말 것. constraint verifier는 형식·필수
    표현만 검사하며 아직 코드를 실제 실행하거나 출처를 확인하지 않는다.
  - frontier-v2 candidate는 aggregate sealed baseline은 넘었지만 한국어·reasoning 성능이 낮다.
    `configs/frontier-v2-candidate-runtime.json`을 active/default로 간주하거나 기존 `frontier-v1-runtime.json`을
    덮어쓰지 말 것. 현재 32-task holdout은 2026-07-15에 label을 열었으므로 v3 선택/튜닝에 재사용 금지.
  - frontier-v3 calibration은 validation 26 route에서 만든 작은 confusion mapping이라 명시적으로 validation-derived다.
    sealed holdout에서 static-GPT를 이기지 못했으므로 `configs/frontier-v3-ko-calibrated-candidate-runtime.json`도
    evidence-only다. v3 holdout label은 2026-07-16에 열렸고 어떤 후속 train/policy/calibration 입력에도 쓰면 안 된다.
  - frontier-v4 영어 candidate도 열린 37-route validation에 선택된 development-only profile이다. 현재 adapter를
    active/default로 보거나 sealed 성능으로 인용하지 말 것. 한국어 사용자 입력을 직접 지원해야 하면 hub가 먼저
    영어 routing brief를 생성해야 하며, 이 router의 한국어 문장력으로 대체하지 말 것.
  - frontier-v4 새 development label은 provider별 winner가 GPT 94 / Claude 74 / Grok 70 / Gemini 9로 불균형하다.
    실제 balanced/capped QLoRA도 static GPT를 못 넘겼으므로 이 분포를 provider 우열이나 static policy로 승격하지 말 것.
    Qwen prompt pilot의 Claude/GPT/Gemini 선택도 2개 과제에서 나온 wiring trace일 뿐이다. dataset verifier는 collection
    후 교정됐으므로 `dataset-manifest.json`의 두 verifier hash와 repair count를 함께 보존한다.
  - Frontier-v4 holdout 64는 연구 종료 시점에도 미개봉이다. 보존 가치가 있지만 현재 objective와 다른 후속 연구에
    억지로 재사용하지 말고, 연구를 다시 열 때 평가 단위가 달라졌다면 새 sealed bank를 만든다.

- **Do-Not-Repeat**
  - 사용자 요청 없이 다른 저장소에 이 시스템을 적용·수정하지 말 것.
  - 생성물(`CLAUDE.md`/`AGENTS.md`/`.gemini/settings.json`) 직접 편집 금지 — `instructions/.ruler/`만 고치고 sync.
  - **orca(및 콕핏 도구)를 포크·패치하지 말 것** — config/MCP/skill로 해결하거나 upstream PR (BUILD-SPEC §8 #11).
  - `.mcp.json`에 실제 API 키를 넣지 말 것.
  - public prior가 그럴듯하다는 이유만으로 primary를 강제 교체하지 말 것. 첫 aggressive 실험은
    label accuracy는 올렸지만 실제 quality regret을 악화시켰다.
  - 현재 holdout을 다시 튜닝 기준으로 쓰지 말 것. prior hash 하나가 아니라 base/adapter/profile/code/evaluator를
    묶은 policy manifest를 고정하고 label-free prediction을 먼저 freeze한 뒤 sealed holdout-v2를 채점한다.
  - Qwen non-fused probe가 VJP 오류를 넘었다는 이유만으로 장시간 학습하지 말 것. 2-step feasibility gate에서
    이미 속도와 memory pressure가 탈락했고 사용자도 이 경로 종료를 결정했다.
  - Prompt-only Qwen smoke가 성공했다는 이유로 active runtime이나 trajectory benchmark를 계속 만들지 말 것.
    연구 재개는 `RESEARCH-CLOSURE.md`의 조건과 사용자의 명시적 결정을 모두 요구한다.
  - frontier-v2 2-task smoke adapter를 운영 profile에 승격하거나 provider 우열 근거로 쓰지 말 것.
  - validation primary만 보고 holdout으로 진입하지 말 것. frontier-v3에서 primary는 올랐지만 validation regret이
    static baseline보다 나쁜 상태를 허용했고 sealed holdout에서 실패했다. 다음 gate는 schema/primary뿐 아니라
    predeclared baseline 대비 mean regret 개선도 동시에 요구한다.
  - 나머지 설계 금지사항은 `BUILD-SPEC.md` §8을 따를 것.

- **다음 한 걸음**
  adaptive `compare` participant 수를 최대 호출 수 계산에 반영하고 각 provider의 실제 답변을 downstream
  context에 전달하는 실패 회귀 테스트부터 추가한다.

<!-- agent-hub:handoff:v1:start -->
## 2026-07-25 Provider 생명주기, 긴 workflow 복구, README 전면 개편

- **원래 목표**: Claude, Grok, Gemini, GPT의 로그인 유지·현재 사용 가능·세션 갱신·재로그인 필요 상태를 공통 GUI에서 안전하게 관리하고, Agent Hub로 저장소 전체를 조사해 README를 새로 작성하며, 조사·작성 중 timeout이나 planner 오류가 발생해도 완료된 작업을 잃지 않고 근본 원인을 해결합니다.
- **현재 단계**: provider auth lifecycle/UI, adaptive timeout 복구, planner 교정, pytest 재현성, README 전면 개편과 전체 검증을 완료했고 구현 커밋 `4ba5db4`를 `origin/main`에 push했습니다. 이 packet은 원격 반영 상태를 기록하는 마지막 인계 갱신입니다.
- **완료**:
  - 공통 redacted 상태에 `logged_in`, `auth_ready`, `refresh_supported`, `refreshable`, `relogin_required`, `connection_state`를 추가하고 GUI에서 `준비됨`, `갱신 가능`, `로그인됨`, `재로그인 필요`, `로그인 필요`를 구분했습니다.
  - Claude·Grok·Gemini refresh에 provider별 exchange lock, consent·credential revision CAS, late-success 차단을 적용했습니다. GPT login/refresh는 공식 Codex 소유로 유지하고 GUI refresh 대상에서 제외했습니다.
  - adaptive 기본 제한을 MCP 1800초, provider 900초, workflow 1740초, 반환 여유 10초로 확대하고 provider timeout을 redacted `provider_call_timeout`으로 정규화했습니다. timeout run은 완료 단계를 보존한 `paused` 상태와 `resumable=true`를 반환합니다.
  - planner validation 교정을 기본 3회, 최대 5회로 늘리고 adaptive/document-write skill을 Codex·Claude Code에 동기화했습니다.
  - `pyproject.toml` pytest `pythonpath`에 저장소 루트를 추가해 외부 `PYTHONPATH` 없이 전체 suite가 수집되도록 고쳤습니다.
  - Agent Hub adaptive plan `e104e320aba5b23a95946ee16cd541a2cf14d1c1aedc40d6da27c537914baf73`에서 Claude/GPT/Gemini `deep/high` 조사 세 개를 병렬 실행하고 Claude가 README를 작성했습니다. 5 leaf call, 386974ms, quality rewrite 1회 후 gate를 통과했습니다.
  - README를 설치, GUI/수동 로그인, live catalog와 `static_fallback`, 공개 도구 37개, fixed/adaptive workflow, 긴 실행 복구, HANDOFF/takeover, 보안, 문제 해결, 개발 검증 중심으로 전면 개편했습니다.
  - 구현·문서·회귀 37개 파일을 `feat: harden provider sessions and workflows` 커밋 `4ba5db4`로 만들고 `origin/main`에 push했습니다.
- **미완**:
  - 실행 중인 Codex plugin MCP는 수정 전 Python 모듈을 이미 import했을 수 있으므로 새 timeout default와 schema를 쓰려면 MCP 재시작이 필요합니다.
  - 실제 Claude·Gemini credential 갱신은 사용자 계정을 바꾸는 동작이라 실행하지 않았습니다.
- **변경 파일**: auth/API/UI는 `src/agent_hub/{operations.py,connect_service.py,connect_app.py,core/auth_state.py,connect_ui/}`와 provider auth 모듈; adaptive 복구는 `src/agent_hub/{operations.py,orchestrator.py}`, 네 provider `mcp_server.py`, `tests/agent_hub/test_adaptive_orchestration.py`; 문서·skill은 `README.md`, `hubs/{shared,codex,claude-code}/skills/{adaptive-orchestrate,document-write,provider-connect}/SKILL.md`; 테스트 재현성은 `pyproject.toml`입니다.
- **검증 실행 결과**:
  - 전체 pytest → `706 passed, 2 skipped`; timeout/provider/auth/UI 집중 suite → `218 passed`
  - 전체 Ruff, `node --check`, 관련 `py_compile`, `git diff --check` → 통과
  - `agent_hub_verify(doc_class=durable,user_facing=true)`와 `orchestrate_codex.document_quality README.md` → 경고 0, 통과
  - hub plugin 검사, Ruler sync 검사, release version 1.4.3 일치, sdist/wheel build → 통과
  - 수정된 source 프로세스의 실제 adaptive README workflow → `completed`, 5 leaf call, 386974ms, quality gate 통과
  - 기존 localhost read-only UI QA → 로그인됨 4, 준비됨 2, 갱신 가능 2, detail focus 유지, console error 0
- **현재 리스크**: 호스트 앱의 외부 timeout이 29분보다 짧으면 긴 단일 MCP 호출이 끊길 수 있으므로 여러 wave 작업은 `start`/`continue`가 안전합니다. timeout 재개는 같은 provider 요청을 다시 보내 사용량이 중복될 수 있습니다. refresh 경쟁은 lock과 CAS로 막지만 실제 credential 갱신은 아직 수행하지 않았습니다.
- **Do-Not-Repeat**: provider timeout을 `adaptive_step_failed`로 굳혀 완료된 wave를 버리지 마세요. 장문 `deep/high` 조사에 150초/270초 제한을 다시 하드코딩하지 마세요. routine status에서 credential을 갱신하거나 token·raw prompt·계정 식별자를 오류, event, DOM, HANDOFF에 넣지 마세요. README에 internal recipe를 public workflow처럼 쓰거나 `static_fallback`을 live catalog로 표현하지 마세요.
- **다음 한 걸음**: Codex에서 Agent Hub MCP를 재시작한 뒤 새 `tools/list`의 `agent_hub_run_workflow` schema에서 `per_call_timeout.default=900`, `workflow_timeout.default=1740`, `planner_repair_attempts.default=3`을 확인하세요.
<!-- agent-hub:handoff:v1:end -->
