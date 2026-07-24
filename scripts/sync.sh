#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT
readonly SOURCE_DIR="${REPO_ROOT}/instructions/.ruler"
readonly BRIDGE_PATH="${REPO_ROOT}/.ruler"
readonly RULER_VERSION="0.3.44"
readonly RULER_PACKAGE="@intellectronica/ruler@${RULER_VERSION}"

created_bridge=0
local_setup_mode="apply"
skip_local_setup=0

cleanup() {
  if [[ "${created_bridge}" -eq 1 && -L "${BRIDGE_PATH}" ]]; then
    rm -- "${BRIDGE_PATH}"
  fi
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for arg in "$@"; do
  case "${arg}" in
    --dry-run)
      local_setup_mode="dry-run"
      ;;
    --help|--version)
      skip_local_setup=1
      ;;
    --verbose|-v)
      ;;
    *)
      echo "error: unsupported sync option: ${arg}" >&2
      echo "allowed options: --dry-run, --verbose, -v, --help, --version" >&2
      exit 2
      ;;
  esac
done

if ! command -v node >/dev/null 2>&1 || ! command -v npx >/dev/null 2>&1; then
  echo "error: Node.js and npx are required" >&2
  exit 1
fi

if [[ ! -d "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/ruler.toml" ]]; then
  echo "error: Ruler source is missing: ${SOURCE_DIR}" >&2
  exit 1
fi

if [[ -L "${BRIDGE_PATH}" ]]; then
  bridge_target="$(cd -- "${BRIDGE_PATH}" && pwd -P)"
  if [[ "${bridge_target}" == "${SOURCE_DIR}" ]]; then
    echo "error: stale temporary bridge exists: ${BRIDGE_PATH}" >&2
    echo "remove that symlink and rerun sync" >&2
  else
    echo "error: ${BRIDGE_PATH} points to another Ruler source" >&2
  fi
  exit 1
elif [[ -e "${BRIDGE_PATH}" ]]; then
  echo "error: refusing to replace existing ${BRIDGE_PATH}" >&2
  exit 1
else
  ln -s -- "${SOURCE_DIR}" "${BRIDGE_PATH}"
  created_bridge=1
fi

cd -- "${REPO_ROOT}"

# --no-nested is required because the source bridge is a directory symlink;
# Ruler 0.3.44 does not support an external --source-dir option.
npx --yes "${RULER_PACKAGE}" apply \
  "$@" \
  --project-root "${REPO_ROOT}" \
  --config "${BRIDGE_PATH}/ruler.toml" \
  --local-only \
  --no-nested \
  --no-gitignore \
  --no-backup \
  --no-mcp \
  --no-skills \
  --no-subagents

if [[ "${skip_local_setup}" -eq 0 ]]; then
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    python_command="${REPO_ROOT}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    python_command="$(command -v python3)"
  else
    echo "error: Python 3 is required to render local MCP config" >&2
    exit 1
  fi
  local_setup_args=(
    -m
    agent_hub.local_setup
    --repo-root
    "${REPO_ROOT}"
    --quiet
  )
  if [[ "${local_setup_mode}" == "apply" ]]; then
    local_setup_args+=(--apply)
  fi
  PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
    "${python_command}" "${local_setup_args[@]}"
fi
