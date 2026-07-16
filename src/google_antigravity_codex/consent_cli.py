"""Local, user-operated consent command for optional integrations."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import stat

from . import security


def grant() -> Path:
    path = security.consent_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    payload = {
        "accepted": True,
        "version": security.CONSENT_FILE_VERSION,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "scope": "optional-antigravity-integrations",
    }
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return path


def revoke() -> Path:
    path = security.consent_file_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    grant_parser = subparsers.add_parser("grant")
    grant_parser.add_argument(
        "--i-understand-and-consent",
        action="store_true",
        help="confirm that the user explicitly chooses to enable the optional integrations",
    )
    subparsers.add_parser("revoke")
    subparsers.add_parser("status")
    args = parser.parse_args(argv)

    if args.command == "grant":
        if not args.i_understand_and_consent:
            parser.error("grant requires --i-understand-and-consent")
        grant()
    elif args.command == "revoke":
        revoke()
    print(json.dumps(security.consent_status(), indent=2, sort_keys=True))
    return 0
