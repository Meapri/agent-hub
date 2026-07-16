# Claude Codex

**버전 0.2.0** · OpenAI **Codex Desktop / GUI**용 플러그인 + **MCP stdio** leaf.

Anthropic **Claude**를 Codex에서 직접 호출합니다. 멀티 프로바이더 오케스트레이션의
실행부(leaf)로 쓰도록 설계했습니다.

> 비공식 프로젝트. Claude / Anthropic 상표는 Anthropic 소유입니다.  
> Hermes 플러그인이 **아닙니다**. [hermes-agent](https://github.com/NousResearch/hermes-agent)에서
> Messages API 변환 아이디어만 참고했습니다. 대응표: [docs/SOURCE_MAP.md](docs/SOURCE_MAP.md)

## 빠른 시작

```bash
# 마켓플레이스 등록
codex plugin marketplace add "/path/to/Claude Codex"
codex plugin add claude-codex@claude-codex

# 동의
python3 scripts/claude_codex_consent.py grant --i-understand-and-consent

# API 키
export ANTHROPIC_API_KEY=sk-ant-...

# 진단
python3 scripts/claude_codex_doctor.py
```


## 구독 로그인 (Claude Max/Pro)

기본 인증은 **Claude Code 구독 OAuth**입니다 (API key 아님).

```bash
claude auth login --claudeai
# macOS Keychain → 파일 미러 (권장)
python3 scripts/claude_codex_login.py mirror-keychain
python3 scripts/claude_codex_consent.py grant --i-understand-and-consent
python3 scripts/claude_codex_login.py status
```

구독 할당량(plan lane)을 쓰려면 hermes-claude-auth와 동일한 **Claude Code 요청 핑거프린트**가
OAuth 요청에 자동 적용됩니다 ([Meapri/hermes-claude-auth](https://github.com/Meapri/hermes-claude-auth)).

API 키 강제: `CLAUDE_CODEX_AUTH_MODE=api_key`

## MCP 도구

| Tool | 역할 |
| --- | --- |
| `claude_codex_consent_status` | 동의 상태 |
| `claude_codex_provider_status` | API 키 준비 여부 (시크릿 없음) |
| `claude_codex_chat` | Messages API 채팅 |
| `claude_codex_list_models` | 모델 목록 |
| `claude_codex_doctor` | 로컬 진단 |

## 환경 변수

| 변수 | 의미 |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API 키 (권장) |
| `ANTHROPIC_BASE_URL` | 기본 `https://api.anthropic.com` |
| `CLAUDE_CODEX_USER_CONSENT=1` | 프로세스 단위 동의 |
| `CLAUDE_CODEX_MODEL` | 기본 모델 오버라이드 |
| `CLAUDE_CODEX_CONFIG_DIR` | 설정 디렉터리 오버라이드 |

## 개발

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## 라이선스

MIT — [LICENSE](LICENSE). Hermes 참고 고지: [NOTICE.md](NOTICE.md).
