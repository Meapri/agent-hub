from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"


def _fake_python(path: Path, *, compatible: bool) -> None:
    compatibility_status = 0 if compatible else 1
    path.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "-c" ]]; then
  exit {compatibility_status}
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  mkdir -p -- "$3/bin"
  cp -- "$0" "$3/bin/python"
  chmod +x "$3/bin/python"
  exit 0
fi
if [[ "${{1:-}}" == "--version" ]]; then
  echo "Python 3.12.0"
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "pip" ]]; then
  printf '%s\\n' "$*" >> "${{AGENT_HUB_BOOTSTRAP_TEST_LOG}}"
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "bootstrap.sh"
    script.write_bytes(BOOTSTRAP.read_bytes())
    script.chmod(0o755)
    return root, script


def test_bootstrap_uses_explicit_compatible_python(tmp_path):
    root, script = _fixture(tmp_path)
    fake_python = tmp_path / "python3.12"
    _fake_python(fake_python, compatible=True)
    log = tmp_path / "pip.log"
    env = {
        **os.environ,
        "AGENT_HUB_PYTHON": str(fake_python),
        "AGENT_HUB_BOOTSTRAP_TEST_LOG": str(log),
    }

    completed = subprocess.run(
        [str(script)],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Using Python 3.12.0" in completed.stdout
    assert f"-m pip install -e {root}[dev]" in log.read_text(encoding="utf-8")


def test_bootstrap_rejects_explicit_old_python(tmp_path):
    root, script = _fixture(tmp_path)
    fake_python = tmp_path / "python3.9"
    _fake_python(fake_python, compatible=False)
    env = {**os.environ, "AGENT_HUB_PYTHON": str(fake_python)}

    completed = subprocess.run(
        [str(script)],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "must be Python 3.11 or newer" in completed.stderr
    assert not (root / ".venv").exists()
