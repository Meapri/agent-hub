#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT

if [[ -x "${REPO_ROOT}/.venv/bin/agent-hub-doctor" ]]; then
  exec "${REPO_ROOT}/.venv/bin/agent-hub-doctor" \
    --repo-root "${REPO_ROOT}" "$@"
fi

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  python_command="${REPO_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  python_command="$(command -v python3)"
else
  echo "error: Python 3.10 or newer is required" >&2
  exit 1
fi

PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
  exec "${python_command}" -m agent_hub.doctor \
    --repo-root "${REPO_ROOT}" "$@"
