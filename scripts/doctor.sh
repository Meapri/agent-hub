#!/usr/bin/env bash
#
# doctor.sh — agent-hub (통합 monorepo) 건강 점검 (읽기 우선).
# 검사: (1) Ruler 지시 정합, (2) 통합 프로젝트 설치(4 패키지 import),
#       (3) basic-memory 기동, (4) MCP console script 4종 존재, (5) 메모리 노트 store.
# 종료 코드: 하나라도 FAIL이면 1. WARN은 0을 유지한다.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
VENV="${REPO_ROOT}/.venv"

fail=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=1; }

echo "[1/5] Ruler 지시 정합 (check-sync.sh)"
if "${SCRIPT_DIR}/check-sync.sh" >/dev/null 2>&1; then
  pass "정본 -> 생성물 동기화 유지"
else
  warn "check-sync 실패 — 최초 커밋 전이면 정상(추적 파일 아님). 아니면 sync.sh 필요"
fi

echo "[2/5] 통합 프로젝트 설치 (4 패키지 import)"
if [ -x "${VENV}/bin/python" ]; then
  if "${VENV}/bin/python" -c "import orchestrate_codex, claude_codex, grok_codex, google_antigravity_codex" 2>/dev/null; then
    pass "orchestrate/claude/grok/antigravity 4 패키지 import OK"
  else
    bad "패키지 import 실패 — .venv/bin/pip install -e '.[dev]' 필요"
  fi
else
  bad ".venv 없음 — python -m venv .venv && .venv/bin/pip install -e '.[dev]'"
fi

echo "[3/5] basic-memory MCP 기동 가능"
if command -v uvx >/dev/null 2>&1; then
  if ver="$(uvx basic-memory --version 2>/dev/null)"; then
    pass "basic-memory 실행 가능 (${ver})"
  else
    warn "uvx는 있으나 basic-memory 실행 실패 — 최초 fetch/네트워크 확인"
  fi
else
  bad "uvx 없음 — uv 설치 필요"
fi

echo "[4/5] MCP console script 4종 존재"
missing=""
for s in orchestrate-codex-mcp claude-codex-mcp grok-codex-mcp google-antigravity-mcp; do
  [ -x "${VENV}/bin/${s}" ] || missing="${missing} ${s}"
done
if [ -z "${missing}" ]; then
  pass "orchestrate/claude/grok/antigravity console script 설치됨"
else
  bad "누락:${missing} — pip install -e . 재실행"
fi

echo "[5/5] 메모리 노트 store (memory/data)"
note_count=0
if [ -d "${REPO_ROOT}/memory/data" ]; then
  note_count="$(find "${REPO_ROOT}/memory/data" -type f -name '*.md' | wc -l | tr -d '[:space:]')"
fi
if [ "${note_count}" -gt 0 ]; then
  pass "노트 ${note_count}개"
else
  warn "노트 없음 — 첫 결정/교훈 기록 권장"
fi

echo
if [ "${fail}" -eq 0 ]; then
  echo "doctor: OK"
else
  echo "doctor: FAIL 있음"
fi
exit "${fail}"
