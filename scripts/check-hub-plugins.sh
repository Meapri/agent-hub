#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT
SOURCE="${REPO_ROOT}/hubs/shared/skills/adaptive-orchestrate/SKILL.md"

for hub in codex claude-code; do
  target="${REPO_ROOT}/hubs/${hub}/skills/adaptive-orchestrate/SKILL.md"
  if ! cmp -s "${SOURCE}" "${target}"; then
    echo "error: ${target} differs from shared adaptive-orchestrate skill" >&2
    exit 1
  fi
done

python3 -m json.tool "${REPO_ROOT}/hubs/codex/.codex-plugin/plugin.json" >/dev/null
python3 -m json.tool "${REPO_ROOT}/hubs/codex/.mcp.json" >/dev/null
python3 -m json.tool "${REPO_ROOT}/hubs/claude-code/.claude-plugin/plugin.json" >/dev/null
python3 -m json.tool "${REPO_ROOT}/hubs/claude-code/.mcp.json" >/dev/null
python3 -m json.tool "${REPO_ROOT}/.agents/plugins/marketplace.json" >/dev/null
python3 -m json.tool "${REPO_ROOT}/.claude-plugin/marketplace.json" >/dev/null

echo "Hub plugin checks passed."
