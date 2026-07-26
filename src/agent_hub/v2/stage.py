"""Digest-fenced staging of immutable versioned Agent Hub runtimes."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Callable, Mapping

from .contracts import canonical_json
from .errors import HubV2Error

DEFAULT_RELEASES_ROOT = Path("~/.agent-hub/releases").expanduser()
_SOURCE_ROOT_FILES = ("pyproject.toml", "README.md", "LICENSE", "NOTICE.md")


def _safe_release_root(path: Path, *, create: bool) -> Path:
    expanded = Path(os.path.abspath(path.expanduser()))
    if expanded in {Path(expanded.anchor), Path.home().resolve()}:
        raise HubV2Error(
            "unsafe_release_root",
            "The release root is too broad.",
            scope="release",
        )
    current = Path(expanded.anchor)
    for part in expanded.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise HubV2Error(
                "unsafe_release_root",
                "The release root contains an unsafe path component.",
                scope="release",
            )
    if create:
        expanded.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(expanded, 0o700)
        return expanded.resolve(strict=True)
    return expanded.resolve(strict=False)


def _source_files(root: Path) -> list[Path]:
    files = [root / name for name in _SOURCE_ROOT_FILES]
    source = root / "src"
    if not source.is_dir():
        raise HubV2Error(
            "invalid_release_source",
            "The release source has no src directory.",
            scope="release",
        )
    files.extend(
        path
        for path in source.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    )
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise HubV2Error(
                "invalid_release_source",
                "The release source is incomplete or unsafe.",
                scope="release",
            )
    return sorted(set(files))


def _source_digest(root: Path) -> tuple[str, int]:
    digest = sha256()
    total = 0
    files = _source_files(root)
    for path in files:
        content = path.read_bytes()
        total += len(content)
        if total > 256 * 1024 * 1024:
            raise HubV2Error(
                "release_source_too_large",
                "The release source exceeds the staging limit.",
                scope="release",
            )
        alias = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(alias).to_bytes(4, "big"))
        digest.update(alias)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest(), len(files)


def _project_version(root: Path) -> str:
    try:
        payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        version = str(payload["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise HubV2Error(
            "invalid_release_source",
            "The release source version is unavailable.",
            scope="release",
        ) from exc
    if not version or any(
        character not in "0123456789abcdefghijklmnopqrstuvwxyz.-" for character in version.lower()
    ):
        raise HubV2Error(
            "invalid_release_source",
            "The release source version is invalid.",
            scope="release",
        )
    return version


def _python_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [
                str(executable),
                "-c",
                "import platform,sys; assert sys.version_info >= (3,10); "
                "print(platform.python_version())",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HubV2Error(
            "release_python_incompatible",
            "Staging requires Python 3.10 or newer.",
            scope="release",
        ) from exc
    return result.stdout.strip()


def _file_digest(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise HubV2Error(
            "release_python_incompatible",
            "The staging Python executable is unsafe.",
            scope="release",
        )
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_stage(
    repo_root: str | Path,
    *,
    releases_root: str | Path = DEFAULT_RELEASES_ROOT,
    python_executable: str | Path = sys.executable,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve(strict=True)
    release_root = _safe_release_root(Path(releases_root), create=False)
    python = Path(python_executable).expanduser().resolve(strict=True)
    source_sha, file_count = _source_digest(root)
    version = _project_version(root)
    destination = release_root / f"{version}-{source_sha[:12]}"
    if destination.exists():
        raise HubV2Error(
            "release_already_staged",
            "The reviewed runtime is already staged.",
            scope="release",
        )
    proposal = {
        "schema": "agent_hub_release_stage_plan_v1",
        "repo_root": str(root),
        "releases_root": str(release_root),
        "destination": str(destination),
        "version": version,
        "source_sha256": source_sha,
        "source_file_count": file_count,
        "python_executable": str(python),
        "python_version": _python_version(python),
        "python_sha256": _file_digest(python),
        "network_access": "pip_dependency_resolution",
    }
    proposal["proposal_sha256"] = sha256(canonical_json(proposal).encode("utf-8")).hexdigest()
    return proposal


def _build_runtime(source: Path, destination: Path, python: Path) -> None:
    subprocess.run(
        [str(python), "-m", "venv", str(destination)],
        check=True,
        timeout=120.0,
    )
    subprocess.run(
        [
            str(destination / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            str(source),
        ],
        check=True,
        timeout=900.0,
    )
    for command in ("agent-hubd", "agent-hub"):
        subprocess.run(
            [str(destination / "bin" / command), "--help"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30.0,
        )


def _relocate_console_shebangs(source_root: Path, destination_root: Path) -> int:
    source_prefix = f"#!{source_root}/bin/python".encode()
    destination_prefix = f"#!{destination_root}/bin/python".encode()
    rewritten = 0
    for path in sorted((source_root / "bin").iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        content = path.read_bytes()
        first, separator, remainder = content.partition(b"\n")
        if not first.startswith(source_prefix):
            continue
        suffix = first[len(source_prefix) :]
        path.write_bytes(destination_prefix + suffix + separator + remainder)
        rewritten += 1
    return rewritten


def apply_stage(
    proposal: Mapping[str, Any],
    *,
    proposal_sha256: str,
    builder: Callable[[Path, Path, Path], None] = _build_runtime,
) -> dict[str, Any]:
    if proposal.get("proposal_sha256") != proposal_sha256:
        raise HubV2Error(
            "proposal_digest_conflict",
            "The release staging proposal digest does not match.",
            scope="release",
        )
    current = plan_stage(
        str(proposal.get("repo_root") or ""),
        releases_root=str(proposal.get("releases_root") or ""),
        python_executable=str(proposal.get("python_executable") or ""),
    )
    if current["proposal_sha256"] != proposal_sha256:
        raise HubV2Error(
            "release_source_conflict",
            "The release source changed after staging was reviewed.",
            scope="release",
            retryable=True,
        )
    release_root = _safe_release_root(Path(current["releases_root"]), create=True)
    destination = Path(current["destination"])
    temporary = Path(tempfile.mkdtemp(prefix=".agent-hub-stage-", dir=release_root))
    try:
        builder(
            Path(current["repo_root"]),
            temporary,
            Path(current["python_executable"]),
        )
        for command in ("agent-hubd", "agent-hub-mcp", "agent-hub"):
            path = temporary / "bin" / command
            if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
                raise HubV2Error(
                    "release_stage_invalid",
                    "The staged runtime is missing a required entrypoint.",
                    scope="release",
                )
        relocated = _relocate_console_shebangs(temporary, destination)
        os.replace(temporary, destination)
        try:
            for command in ("agent-hubd", "agent-hub-mcp", "agent-hub"):
                subprocess.run(
                    [str(destination / "bin" / command), "--help"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30.0,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            shutil.rmtree(destination)
            raise HubV2Error(
                "release_stage_invalid",
                "The relocated runtime entrypoints failed validation.",
                scope="release",
            ) from exc
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return {
        "schema": "agent_hub_release_stage_result_v1",
        "success": True,
        "version": current["version"],
        "source_sha256": current["source_sha256"],
        "runtime_root": str(destination),
        "candidate_daemon": str(destination / "bin" / "agent-hubd"),
        "relocated_shebang_count": relocated,
    }
