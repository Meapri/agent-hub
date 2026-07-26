# Grok Codex

> **보관용 snapshot입니다.** 현재 Agent Hub 설치·실행 경로가 아니며 아래 독립 플러그인 명령은
> 통합 전 구조를 기록하기 위해 남아 있습니다. 실제 설치와 provider 관리는 저장소 루트의
> `README.md`, `agent-hub setup`, `agent-hub-connect`를 사용하세요.

**버전 0.2.0** · OpenAI **Codex Desktop / GUI**용 플러그인 + **MCP stdio** leaf.

xAI **Grok**를 Codex에서 직접 호출합니다. 오케스트레이션 플러그인의 leaf로 쓰기 좋게
얇게 만들었습니다.

> 비공식 프로젝트. Grok / xAI 상표는 xAI 소유입니다.  
> Hermes가 **아닙니다**. [hermes-agent](https://github.com/NousResearch/hermes-agent)에서
> xAI 엔드포인트·헤더·Responses 아이디어만 참고. [docs/SOURCE_MAP.md](docs/SOURCE_MAP.md)

## 빠른 시작

```bash
codex plugin marketplace add "/path/to/Grok Codex"
codex plugin add grok-codex@grok-codex

python3 scripts/grok_codex_consent.py grant --i-understand-and-consent
export XAI_API_KEY=xai-...

python3 scripts/grok_codex_doctor.py
```


## 구독 로그인 (SuperGrok / X Premium+)

기본 인증은 **xAI device-code OAuth** (Hermes `xai-oauth`와 동일 패턴).

```bash
python3 scripts/grok_codex_consent.py grant --i-understand-and-consent
python3 scripts/grok_codex_login.py interactive
# 브라우저에서 accounts.x.ai 승인
python3 scripts/grok_codex_login.py status
```

MCP: `grok_codex_login_start` → 브라우저 승인 → `grok_codex_login_complete`

API 키 강제: `GROK_CODEX_AUTH_MODE=api_key` + `XAI_API_KEY`

## MCP 도구

| Tool | 역할 |
| --- | --- |
| `grok_codex_consent_status` | 동의 상태 |
| `grok_codex_provider_status` | API 키 준비 여부 |
| `grok_codex_chat` | Chat Completions (기본) 또는 Responses |
| `grok_codex_list_models` | 모델 목록 |
| `grok_codex_doctor` | 로컬 진단 |

`grok_codex_chat` 옵션:

- `api_mode`: `chat` (기본) | `responses`
- `session_id`: 대화 친화 캐시용 (`x-grok-conv-id`)

## 환경 변수

| 변수 | 의미 |
| --- | --- |
| `XAI_API_KEY` | xAI API 키 |
| `XAI_BASE_URL` | 기본 `https://api.x.ai/v1` |
| `GROK_CODEX_USER_CONSENT=1` | 프로세스 단위 동의 |
| `GROK_CODEX_MODEL` | 기본 모델 |
| `GROK_CODEX_API_MODE` | `chat` 또는 `responses` |
| `GROK_CODEX_CONFIG_DIR` | 설정 디렉터리 |

## 개발

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## 라이선스

MIT — [LICENSE](LICENSE). [NOTICE.md](NOTICE.md).
