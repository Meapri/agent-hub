"""Digest-fenced macOS LaunchAgent and v2 host bridge setup."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
from typing import Any, Mapping

from agent_hub import local_setup

from .errors import HubV2Error

LAUNCH_AGENT_LABEL = "com.agent-hub.daemon"
LAUNCH_AGENT_NAME = f"{LAUNCH_AGENT_LABEL}.plist"


def _safe_optional_file(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HubV2Error(
            "unsafe_setup_target",
            "The LaunchAgent target is not a safe regular file.",
            scope="setup",
        )
    return path.read_bytes()


def _launch_agent_bytes(repo_root: Path, state_root: Path) -> bytes:
    executable = repo_root / ".venv" / "bin" / "agent-hubd"
    socket_path = state_root / "run" / "agent-hub.sock"
    state_db = state_root / "state.sqlite3"
    logs = state_root / "logs"
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(executable),
            "--socket",
            str(socket_path),
            "--state-db",
            str(state_db),
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {
            "PATH": ":".join(
                [
                    str(repo_root / ".venv" / "bin"),
                    str(Path.home() / ".local" / "bin"),
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                ]
            )
        },
        "StandardOutPath": str(logs / "daemon.out.log"),
        "StandardErrorPath": str(logs / "daemon.err.log"),
        "ProcessType": "Interactive",
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def plan_setup(
    repo_root: str | Path,
    *,
    target_root: str | Path | None = None,
    launch_agents_dir: str | Path | None = None,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve(strict=True)
    target = (
        Path(target_root).expanduser().resolve(strict=True)
        if target_root
        else root
    )
    launch_dir = (
        Path(launch_agents_dir).expanduser()
        if launch_agents_dir
        else Path("~/Library/LaunchAgents").expanduser()
    )
    state = (
        Path(state_root).expanduser()
        if state_root
        else Path("~/.agent-hub").expanduser()
    )
    host_plan = local_setup.plan_setup(
        root,
        target_root=target,
        hub_executable="agent-hub-mcp",
    )
    launch_path = launch_dir / LAUNCH_AGENT_NAME
    existing = _safe_optional_file(launch_path)
    rendered = _launch_agent_bytes(root, state)
    proposal = {
        "schema": "agent_hub_v2_setup_plan_v1",
        "repo_root": str(root),
        "target_root": str(target),
        "state_root": str(state),
        "host_config": host_plan.public(),
        "launch_agent": {
            "path": str(launch_path),
            "before_sha256": sha256(existing).hexdigest() if existing else None,
            "after_sha256": sha256(rendered).hexdigest(),
            "status": (
                "create"
                if existing is None
                else "unchanged"
                if existing == rendered
                else "update"
            ),
            "rendered": rendered.decode("utf-8"),
        },
        "apply_required": bool(host_plan.changed or existing != rendered),
        "network_access": False,
        "provider_auth_mutation": False,
    }
    proposal["proposal_sha256"] = sha256(
        (
            str(proposal["host_config"])
            + str(proposal["launch_agent"]["before_sha256"])
            + proposal["launch_agent"]["after_sha256"]
        ).encode("utf-8")
    ).hexdigest()
    proposal["_host_plan"] = host_plan
    return proposal


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise HubV2Error(
            "unsafe_setup_target",
            "The LaunchAgent directory must not be a symlink.",
            scope="setup",
        )
    descriptor, temp_name = tempfile.mkstemp(prefix=".agent-hub.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def apply_setup(
    proposal: Mapping[str, Any],
    *,
    proposal_sha256: str,
    activate: bool = True,
) -> dict[str, Any]:
    if proposal.get("proposal_sha256") != proposal_sha256:
        raise HubV2Error(
            "proposal_digest_conflict",
            "The setup proposal digest does not match.",
            scope="setup",
        )
    host_plan = proposal.get("_host_plan")
    if not isinstance(host_plan, local_setup.SetupPlan):
        raise HubV2Error(
            "invalid_setup_plan",
            "The setup plan is not an in-process reviewed plan.",
            scope="setup",
        )
    launch = proposal.get("launch_agent")
    if not isinstance(launch, Mapping):
        raise HubV2Error(
            "invalid_setup_plan",
            "The LaunchAgent plan is missing.",
            scope="setup",
        )
    launch_path = Path(str(launch["path"])).expanduser()
    current = _safe_optional_file(launch_path)
    current_sha = sha256(current).hexdigest() if current else None
    if current_sha != launch.get("before_sha256"):
        raise HubV2Error(
            "setup_target_conflict",
            "The LaunchAgent changed after planning.",
            scope="setup",
            retryable=True,
        )
    host_result = local_setup.apply_plan(host_plan)
    rendered = str(launch.get("rendered") or "").encode("utf-8")
    if sha256(rendered).hexdigest() != launch.get("after_sha256"):
        raise HubV2Error(
            "proposal_digest_conflict",
            "The LaunchAgent content changed after planning.",
            scope="setup",
        )
    state_root = Path(str(proposal.get("state_root") or "")).expanduser()
    for directory in (state_root, state_root / "run", state_root / "logs"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    _atomic_write(launch_path, rendered)
    activation = {
        "attempted": False,
        "success": False,
        "next_action": {
            "type": "command",
            "command": f"launchctl bootstrap gui/{os.getuid()} {launch_path}",
        },
    }
    if activate:
        subprocess.run(
            [
                "/bin/launchctl",
                "bootout",
                f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        result = subprocess.run(
            [
                "/bin/launchctl",
                "bootstrap",
                f"gui/{os.getuid()}",
                str(launch_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        if result.returncode == 0:
            kickstart = subprocess.run(
                [
                    "/bin/launchctl",
                    "kickstart",
                    "-k",
                    f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15.0,
            )
        else:
            kickstart = None
        activation = {
            "attempted": True,
            "success": result.returncode == 0
            and kickstart is not None
            and kickstart.returncode == 0,
            "returncode": result.returncode,
            "kickstart_returncode": (
                kickstart.returncode if kickstart is not None else None
            ),
            "next_action": (
                None
                if (
                    result.returncode == 0
                    and kickstart is not None
                    and kickstart.returncode == 0
                )
                else {
                    "type": "command",
                    "command": (
                        f"launchctl kickstart -k "
                        f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"
                    ),
                }
            ),
        }
    return {
        "schema": "agent_hub_v2_setup_result_v1",
        "success": not activate or bool(activation["success"]),
        "host_config": host_result,
        "launch_agent": {
            "path": str(launch_path),
            "sha256": sha256(rendered).hexdigest(),
        },
        "activation": activation,
    }
