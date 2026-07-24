#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT
SOURCE_ROOT="${REPO_ROOT}/hubs/shared/skills"
runtime_root="$(mktemp -d "${TMPDIR:-/tmp}/agent-hub-plugin-check.XXXXXX")"

cleanup() {
  rm -rf -- "${runtime_root}"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for source in "${SOURCE_ROOT}"/*/SKILL.md; do
  skill="$(basename -- "$(dirname -- "${source}")")"
  for hub in codex claude-code; do
    target="${REPO_ROOT}/hubs/${hub}/skills/${skill}/SKILL.md"
    if ! cmp -s "${source}" "${target}"; then
      echo "error: ${target} differs from shared ${skill} skill" >&2
      exit 1
    fi
  done
done

python3 -m json.tool "${REPO_ROOT}/hubs/codex/.codex-plugin/plugin.json" >/dev/null
python3 -m json.tool "${REPO_ROOT}/hubs/claude-code/.claude-plugin/plugin.json" >/dev/null
python3 -m json.tool "${REPO_ROOT}/.agents/plugins/marketplace.json" >/dev/null
python3 -m json.tool "${REPO_ROOT}/.claude-plugin/marketplace.json" >/dev/null

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  python_command="${REPO_ROOT}/.venv/bin/python"
else
  python_command="python3"
fi
PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
  "${python_command}" -m agent_hub.local_setup \
    --repo-root "${REPO_ROOT}" \
    --target-root "${runtime_root}" \
    --apply \
    --quiet >/dev/null
python3 -m json.tool "${runtime_root}/hubs/codex/.mcp.json" >/dev/null
python3 -m json.tool "${runtime_root}/hubs/claude-code/.mcp.json" >/dev/null

echo "Hub plugin checks passed."
