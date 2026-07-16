# Google Antigravity Codex

**버전 0.9.8** · OpenAI **Codex Desktop / GUI**용 플러그인(`.codex-plugin`) + **MCP stdio** 서버.

Codex 안에서 Google Antigravity(Code Assist)로 채팅, Google Search grounding, 이미지 생성, 글쓰기, 모델 선택, diff 리뷰 등을 쓸 수 있습니다.

> **비공식 프로젝트**  
> Google, Gemini, Antigravity 상표는 Google LLC 소유입니다. 이 저장소는 비공식 오픈소스입니다.  
> 저장소: [Meapri/google-antigravity-codex](https://github.com/Meapri/google-antigravity-codex)

---

## 이 프로젝트가 아닌 것

**Hermes 플러그인이 아닙니다.**

| 참고만 한 업스트림 | 이 프로젝트 |
| --- | --- |
| [Meapri/hermes-google-antigravity-plugin](https://github.com/Meapri/hermes-google-antigravity-plugin) | Codex용 `.codex-plugin` + skills + MCP |
| [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent) | 요청 형식·그라운딩/이미지 아이디어만 참고 |

Hermes 대응 메모: [docs/SOURCE_MAP.md](docs/SOURCE_MAP.md)

---

## 요구 사항

- **Python 3.9+**
- **macOS** 우선 (현재 주 지원 환경)
- Codex Desktop / GUI (플러그인·MCP 로드)

---

## 빠른 시작

### 1. 플러그인 설치

```bash
# 클론한 저장소 경로를 로컬 마켓플레이스로 등록
codex plugin marketplace add "/path/to/google-antigravity-codex"

# 플러그인 설치 (스킬 + MCP)
codex plugin add google-antigravity-codex@google-antigravity-codex
```

설치 후 **Codex 앱을 재시작**하거나 새 스레드를 열어 스킬/MCP가 로드되게 하세요.

```bash
codex plugin list | grep antigravity
```

### 2. 동의 (필수)

모델·API 호출 MCP 도구는 로컬 동의가 있어야 동작합니다.

```bash
python3 scripts/google_antigravity_consent.py grant --i-understand-and-consent
python3 scripts/google_antigravity_consent.py status
```

프로세스 단위 임시 동의: `GOOGLE_ANTIGRAVITY_USER_CONSENT=1`

### 3. Google 로그인 (플러그인 OAuth)

브라우저 Google 로그인(PKCE). **agy CLI 세션/Keychain을 API에 끌어오지 않습니다.**

```bash
python3 scripts/google_antigravity_login.py interactive
```

또는 Codex MCP:

1. `google_antigravity_login_start` → `auth_url` 열기  
2. `google_antigravity_login_complete` 에 리다이렉트 URL 또는 `code` 전달  

토큰 파일: `~/.config/google-antigravity-codex/oauth-token.json`  
로그인 후 전송 경로는 **`agy-oauth`** (Cloud Code PA)입니다.

### 4. 상태 점검

```bash
python3 scripts/google_antigravity_doctor.py --json
```

라이브 기능 확인(모델 목록 · grounded search · 이미지):

```bash
python3 scripts/verify_live_features.py
```

---

## 인증 구조

```text
Codex GUI / MCP client
        │
        ▼  동의(consent) 게이트
플러그인 MCP (scripts/google_antigravity_mcp.py)
        │
        ▼  직접 Google OAuth (PKCE)
agy-oauth  →  cloudcode-pa.googleapis.com  (Code Assist)
        │
        ▼
~/.config/google-antigravity-codex/oauth-token.json
```

- **chat / grounded search / image / write** 등 API 호출은 플러그인 OAuth 토큰만 사용합니다.
- 공식 `agy` CLI 로그인 세션·시스템 Keychain은 **API 경로에서 재사용하지 않습니다.**

---

## Codex에서 쓰기

1. 위 순서로 설치 · 동의 · 로그인  
2. Codex에서 스킬을 고르거나 MCP 도구를 호출  
3. 모델 선호는 `google_antigravity_set_model` / 프로필 도구로 저장 가능  

MCP 엔트리포인트: `scripts/google_antigravity_mcp.py`  
패키지 콘솔 스크립트: `google-antigravity-mcp`, `google-antigravity-consent`

---

## MCP 도구 (28)

도구 이름은 모두 `google_antigravity_*` 접두사를 씁니다. (검색 도구만 `google_grounded_search`)

| 분류 | 도구 | 역할 |
| --- | --- | --- |
| **동의·상태** | `google_antigravity_consent_status` | 동의 상태 (부여 불가) |
| | `google_antigravity_provider_status` | `agy-oauth` 프로바이더 진단 (시크릿 없음) |
| | `google_antigravity_agy_auth_status` | 플러그인 OAuth 토큰 파일 존재 여부 |
| | `google_antigravity_agy_auth_refresh` | refresh token으로 access token 갱신 |
| | `google_antigravity_quota_status` | 준비 상태 (통합 쿼터 버킷 없음) |
| **로그인** | `google_antigravity_login_status` | OAuth 로그인 상태 |
| | `google_antigravity_login_start` | PKCE auth URL 발급 |
| | `google_antigravity_login_complete` | 리다이렉트 URL/code로 완료 |
| | `google_antigravity_whoami` | 이메일·project (시크릿 없음) |
| | `google_antigravity_logout` | 로컬 플러그인 토큰 삭제 |
| **핵심** | `google_antigravity_chat` | 채팅 (멀티모달·tool-calls·stream 옵션) |
| | `google_grounded_search` | Google Search grounding |
| | `google_antigravity_generate_image` | 이미지 생성·로컬 캐시 저장 |
| | `google_antigravity_write` | 초안·윤문·번역·요약·README 등. **readme/technical-doc**는 durable(fact pack, git diary off). 다단계 README는 orchestrate-codex `durable_readme` |
| **릴리스** | `google_antigravity_release_snapshot` | 로컬 git 릴리스 컨텍스트 |
| | `google_antigravity_release_draft` | PR/릴리스 노트 초안 |
| **모델** | `google_antigravity_list_models` | 텍스트·이미지 모델 목록 |
| | `google_antigravity_route_model` | 작업→모델·도구 추천 |
| | `google_antigravity_get_model_prefs` | 저장된 모델 선호 조회 |
| | `google_antigravity_set_model` | 기본/태스크별 모델 저장 |
| | `google_antigravity_clear_model_prefs` | 모델 선호 삭제 |
| | `google_antigravity_set_provider` | 전송 경로 선호 (`agy-oauth`) |
| | `google_antigravity_get_session_prefs` | 프로바이더·활성 프로필 |
| **프로필** | `google_antigravity_list_profiles` | 빌트인·커스텀 프로필 |
| | `google_antigravity_use_profile` | 프로필 활성화 |
| | `google_antigravity_save_profile` | 커스텀 프로필 저장 |
| **품질** | `google_antigravity_compare_models` | 2–3 모델 짧은 비교 |
| | `google_antigravity_review_diff` | 로컬 git diff 리뷰 |

**이미지 요청:** Code Assist는 `generationConfig.responseModalities: ["IMAGE"]` 만 허용합니다. `responseFormat.image` 필드는 HTTP 400을 유발하므로 사용하지 않습니다.

---

## 스킬 (`skills/`)

| 스킬 | 용도 |
| --- | --- |
| `google-antigravity` | 개요·진입 |
| `antigravity-login` | 동의·OAuth 로그인 |
| `antigravity-doctor` | 진단 |
| `antigravity-coding` | 코딩 보조 |
| `antigravity-code-review` | 코드 리뷰 |
| `antigravity-model-router` | 작업→모델 라우팅 |
| `antigravity-model-picker` | 모델 선호 설정 |
| `antigravity-profiles` | 세션 프로필 |
| `antigravity-pair` | 페어 모드 |
| `antigravity-research` | 리서치·그라운딩 |
| `google-grounded-search` | 검색 grounding |
| `antigravity-image` | 이미지 생성 |
| `gemini-writing` | 글쓰기 |
| `release-copilot` | 릴리스/PR 초안 |

---

## 환경 변수

| 변수 | 의미 |
| --- | --- |
| `GOOGLE_ANTIGRAVITY_USER_CONSENT=1` | 파일 동의 없이 프로세스 단위 동의 |
| `GOOGLE_ANTIGRAVITY_PROVIDER=agy-oauth` | 전송 경로 강제 (설정 시 우선) |
| `GOOGLE_ANTIGRAVITY_IMAGE_MODEL` | 이미지 모델 오버라이드 (선택) |
| `GOOGLE_ANTIGRAVITY_WRITING_MODEL` | 글쓰기 모델 오버라이드 (선택) |

---

## 보안

- 모델 HTTP는 고정 호스트 `cloudcode-pa.googleapis.com` 만 사용
- 응답·이미지 크기 제한, 리다이렉트/프록시 가드
- 토큰 파일: 일반 파일, 소유자 일치, 권한 `0600` 권장
- 자세한 내용: [docs/security.md](docs/security.md)

---

## 개발 / 테스트

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/python scripts/check_release_version.py
# 선택: 플러그인 번들
.venv/bin/python scripts/build_plugin_bundle.py
```

---

## 라이선스

[MIT](LICENSE)
