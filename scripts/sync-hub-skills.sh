#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT
SOURCE="${REPO_ROOT}/hubs/shared/skills/adaptive-orchestrate/SKILL.md"

for hub in codex claude-code; do
  target="${REPO_ROOT}/hubs/${hub}/skills/adaptive-orchestrate/SKILL.md"
  mkdir -p -- "$(dirname -- "${target}")"
  cp -- "${SOURCE}" "${target}"
done

echo "Hub adaptive-orchestrate skill synchronized."
