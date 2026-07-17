#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT
SOURCE_ROOT="${REPO_ROOT}/hubs/shared/skills"

for source in "${SOURCE_ROOT}"/*/SKILL.md; do
  skill="$(basename -- "$(dirname -- "${source}")")"
  for hub in codex claude-code; do
    target="${REPO_ROOT}/hubs/${hub}/skills/${skill}/SKILL.md"
    mkdir -p -- "$(dirname -- "${target}")"
    cp -- "${source}" "${target}"
  done
done

echo "Hub shared skills synchronized."
