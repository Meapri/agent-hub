#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT
readonly VENV_DIR="${REPO_ROOT}/.venv"

python_is_compatible() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1
}

resolve_python() {
  if [[ -n "${AGENT_HUB_PYTHON:-}" ]]; then
    if ! command -v -- "${AGENT_HUB_PYTHON}" >/dev/null 2>&1; then
      echo "error: AGENT_HUB_PYTHON is not executable: ${AGENT_HUB_PYTHON}" >&2
      return 1
    fi
    if ! python_is_compatible "${AGENT_HUB_PYTHON}"; then
      echo "error: AGENT_HUB_PYTHON must be Python 3.10 or newer" >&2
      return 1
    fi
    command -v -- "${AGENT_HUB_PYTHON}"
    return
  fi

  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v -- "${candidate}" >/dev/null 2>&1 \
      && python_is_compatible "${candidate}"; then
      command -v -- "${candidate}"
      return
    fi
  done

  echo "error: Python 3.10 or newer is required" >&2
  echo "install a supported Python or set AGENT_HUB_PYTHON to its executable" >&2
  return 1
}

python_command="$(resolve_python)"
readonly python_command

if [[ -x "${VENV_DIR}/bin/python" ]]; then
  if ! python_is_compatible "${VENV_DIR}/bin/python"; then
    echo "error: existing .venv uses Python older than 3.10" >&2
    echo "move or remove ${VENV_DIR} after reviewing it, then run bootstrap again" >&2
    exit 1
  fi
else
  "${python_command}" -m venv "${VENV_DIR}"
fi

echo "Using $("${VENV_DIR}/bin/python" --version 2>&1) at ${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install -e "${REPO_ROOT}[dev]"
