"""Atomic LaunchAgent executable switching with a local rollback slot."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping

from .daemon import HubDaemonClient
from .errors import HubV2Error
from .setup import LAUNCH_AGENT_LABEL, LAUNCH_AGENT_NAME

ROLLBACK_DIR = Path("~/.agent-hub/rollback").expanduser()
ROLLBACK_PLIST = ROLLBACK_DIR / LAUNCH_AGENT_NAME


def _safe_file(path: Path, *, executable: bool = False) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise HubV2Error(
            "release_target_unavailable",
            "The release target does not exist.",
            scope="release",
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HubV2Error(
            "unsafe_release_target",
            "The release target must be a safe regular file.",
            scope="release",
        )
    if executable and not os.access(path, os.X_OK):
        raise HubV2Error(
            "release_target_not_executable",
            "The candidate daemon is not executable.",
            scope="release",
        )
    return path.read_bytes()


def _launch_path(value: str | Path | None) -> Path:
    return (
        Path(value).expanduser()
        if value
        else Path("~/Library/LaunchAgents").expanduser() / LAUNCH_AGENT_NAME
    )


def _proposal(
    *,
    mode: str,
    launch_path: Path,
    before: bytes,
    after: bytes,
    previous_executable: str,
    proposed_executable: str,
) -> dict[str, Any]:
    proposal = {
        "schema": "agent_hub_release_switch_plan_v1",
        "mode": mode,
        "launch_agent_path": str(launch_path),
        "before_sha256": sha256(before).hexdigest(),
        "after_sha256": sha256(after).hexdigest(),
        "previous_executable": previous_executable,
        "proposed_executable": proposed_executable,
        "health_check": "temporary_socket_ping",
        "rollback_slot": str(ROLLBACK_PLIST),
    }
    proposal["proposal_sha256"] = sha256(
        (
            mode
            + proposal["before_sha256"]
            + proposal["after_sha256"]
            + proposed_executable
        ).encode("utf-8")
    ).hexdigest()
    proposal["_before"] = before
    proposal["_after"] = after
    return proposal


def plan_update(
    candidate_root: str | Path,
    *,
    launch_agent_path: str | Path | None = None,
) -> dict[str, Any]:
    launch_path = _launch_path(launch_agent_path)
    before = _safe_file(launch_path)
    try:
        current = plistlib.loads(before)
    except plistlib.InvalidFileException as exc:
        raise HubV2Error(
            "invalid_launch_agent",
            "The installed LaunchAgent is invalid.",
            scope="release",
        ) from exc
    arguments = current.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments:
        raise HubV2Error(
            "invalid_launch_agent",
            "The installed LaunchAgent has no daemon command.",
            scope="release",
        )
    candidate = Path(candidate_root).expanduser().resolve(strict=True) / "bin/agent-hubd"
    _safe_file(candidate, executable=True)
    previous = str(arguments[0])
    updated = dict(current)
    updated["ProgramArguments"] = [str(candidate), *arguments[1:]]
    after = plistlib.dumps(updated, fmt=plistlib.FMT_XML, sort_keys=True)
    return _proposal(
        mode="update",
        launch_path=launch_path,
        before=before,
        after=after,
        previous_executable=previous,
        proposed_executable=str(candidate),
    )


def plan_rollback(
    *,
    launch_agent_path: str | Path | None = None,
    rollback_path: str | Path = ROLLBACK_PLIST,
) -> dict[str, Any]:
    launch_path = _launch_path(launch_agent_path)
    before = _safe_file(launch_path)
    after = _safe_file(Path(rollback_path).expanduser())
    try:
        current = plistlib.loads(before)
        rollback = plistlib.loads(after)
        current_args = current["ProgramArguments"]
        rollback_args = rollback["ProgramArguments"]
    except (plistlib.InvalidFileException, KeyError, TypeError) as exc:
        raise HubV2Error(
            "invalid_launch_agent",
            "The current or rollback LaunchAgent is invalid.",
            scope="release",
        ) from exc
    _safe_file(Path(str(rollback_args[0])), executable=True)
    return _proposal(
        mode="rollback",
        launch_path=launch_path,
        before=before,
        after=after,
        previous_executable=str(current_args[0]),
        proposed_executable=str(rollback_args[0]),
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".release.", dir=path.parent)
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


def _replace_argument(arguments: list[str], flag: str, value: str) -> list[str]:
    updated = list(arguments)
    if flag in updated:
        index = updated.index(flag)
        if index + 1 >= len(updated):
            raise HubV2Error(
                "invalid_launch_agent",
                "The LaunchAgent has an incomplete daemon argument.",
                scope="release",
            )
        updated[index + 1] = value
    else:
        updated.extend([flag, value])
    return updated


def _candidate_health(after: bytes) -> dict[str, Any]:
    try:
        payload = plistlib.loads(after)
        arguments = [str(item) for item in payload["ProgramArguments"]]
    except (plistlib.InvalidFileException, KeyError, TypeError) as exc:
        raise HubV2Error(
            "invalid_launch_agent",
            "The candidate LaunchAgent is invalid.",
            scope="release",
        ) from exc
    with tempfile.TemporaryDirectory(prefix="agent-hub-release-health-") as directory:
        root = Path(directory)
        socket_path = root / "run" / "agent-hub.sock"
        state_db = root / "state.sqlite3"
        arguments = _replace_argument(arguments, "--socket", str(socket_path))
        arguments = _replace_argument(arguments, "--state-db", str(state_db))
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                if socket_path.exists():
                    try:
                        response = HubDaemonClient(socket_path).request(
                            "ping",
                            timeout=1.0,
                        )
                    except HubV2Error:
                        pass
                    else:
                        if response.get("success") is True:
                            return {
                                "schema": "agent_hub_candidate_health_v1",
                                "success": True,
                                "check": "temporary_socket_ping",
                            }
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
        raise HubV2Error(
            "candidate_health_check_failed",
            "The candidate daemon failed its isolated socket health check.",
            scope="release",
        )


def _installed_socket(after: bytes) -> Path:
    payload = plistlib.loads(after)
    arguments = [str(item) for item in payload["ProgramArguments"]]
    if "--socket" not in arguments:
        return Path("~/.agent-hub/run/agent-hub.sock").expanduser()
    index = arguments.index("--socket")
    if index + 1 >= len(arguments):
        raise HubV2Error(
            "invalid_launch_agent",
            "The LaunchAgent has an incomplete socket argument.",
            scope="release",
        )
    return Path(arguments[index + 1]).expanduser()


def _wait_installed_health(after: bytes) -> bool:
    socket_path = _installed_socket(after)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            response = HubDaemonClient(socket_path).request("ping", timeout=1.0)
        except HubV2Error:
            time.sleep(0.1)
            continue
        return response.get("success") is True
    return False


def apply_switch(
    proposal: Mapping[str, Any],
    *,
    proposal_sha256: str,
    activate: bool = True,
    rollback_path: str | Path = ROLLBACK_PLIST,
) -> dict[str, Any]:
    if proposal.get("proposal_sha256") != proposal_sha256:
        raise HubV2Error(
            "proposal_digest_conflict",
            "The release proposal digest does not match.",
            scope="release",
        )
    before = proposal.get("_before")
    after = proposal.get("_after")
    if not isinstance(before, bytes) or not isinstance(after, bytes):
        raise HubV2Error(
            "invalid_release_plan",
            "The release plan is not an in-process reviewed plan.",
            scope="release",
        )
    launch_path = Path(str(proposal.get("launch_agent_path") or "")).expanduser()
    current = _safe_file(launch_path)
    if sha256(current).hexdigest() != proposal.get("before_sha256"):
        raise HubV2Error(
            "release_target_conflict",
            "The LaunchAgent changed after planning.",
            scope="release",
            retryable=True,
        )
    health = _candidate_health(after)
    rollback = Path(rollback_path).expanduser()
    if proposal.get("mode") == "update":
        _atomic_write(rollback, before)
    _atomic_write(launch_path, after)
    activation = {"attempted": False, "success": False}
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
        active = result.returncode == 0 and _wait_installed_health(after)
        activation = {
            "attempted": True,
            "success": active,
            "returncode": result.returncode,
        }
        if not active:
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
            _atomic_write(launch_path, before)
            subprocess.run(
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
            raise HubV2Error(
                "release_activation_failed",
                "The daemon restart failed and the LaunchAgent was rolled back.",
                scope="release",
                retryable=True,
            )
    return {
        "schema": "agent_hub_release_switch_result_v1",
        "success": True,
        "mode": proposal["mode"],
        "previous_executable": proposal["previous_executable"],
        "active_executable": proposal["proposed_executable"],
        "launch_agent_sha256": sha256(after).hexdigest(),
        "activation": activation,
        "candidate_health": health,
        "rollback_available": rollback.exists(),
    }
