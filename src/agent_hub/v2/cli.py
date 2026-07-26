"""Unified v2 lifecycle CLI. Mutating commands are dry-run unless --apply."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .context import index_project
from .daemon import DEFAULT_SOCKET_PATH, HubDaemonClient
from .errors import HubV2Error
from .policy import apply_policy_update, load_policy, prepare_policy_update
from .release import ROLLBACK_PLIST, apply_switch, plan_rollback, plan_update
from .repair import apply_repair, plan_repair
from .setup import apply_setup, plan_setup
from .stage import DEFAULT_RELEASES_ROOT, apply_stage, plan_stage
from .store import DEFAULT_DB_NAME, DEFAULT_STATE_DIR, HubStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the Agent Hub v2 local runtime.")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Prepare or create .agent-hub/project.toml.")
    init.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    init.add_argument("--project-root", default=".")
    init.add_argument("--apply", action="store_true")
    init.add_argument("--proposal-sha256")

    setup = sub.add_parser("setup", help="Prepare or apply v2 host and LaunchAgent wiring.")
    setup.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    setup.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[3]))
    setup.add_argument("--target-root")
    setup.add_argument("--launch-agents-dir")
    setup.add_argument("--state-root")
    setup.add_argument("--runtime-root")
    setup.add_argument("--apply", action="store_true")
    setup.add_argument("--proposal-sha256")
    setup.add_argument("--no-activate", action="store_true")

    doctor = sub.add_parser("doctor", help="Query the running daemon health.")
    doctor.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    doctor.add_argument("--socket", default=str(DEFAULT_SOCKET_PATH))
    doctor.add_argument("--project-root", default=".")
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--repair", action="store_true")

    repair = sub.add_parser("repair", help="Prepare or apply a digest-fenced repair.")
    repair.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    repair.add_argument(
        "--state-db",
        default=str(DEFAULT_STATE_DIR / DEFAULT_DB_NAME),
    )
    repair.add_argument("--apply", action="store_true")
    repair.add_argument("--proposal-sha256")

    context = sub.add_parser("index", help="Build the local FTS5 project index.")
    context.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    context.add_argument("--project-root", default=".")
    context.add_argument(
        "--state-db",
        default=str(DEFAULT_STATE_DIR / DEFAULT_DB_NAME),
    )

    update = sub.add_parser("update", help="Prepare or apply an atomic daemon switch.")
    update.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    update.add_argument("--candidate-root", required=True)
    update.add_argument("--launch-agent-path")
    update.add_argument("--rollback-path")
    update.add_argument("--apply", action="store_true")
    update.add_argument("--proposal-sha256")
    update.add_argument("--no-activate", action="store_true")

    stage = sub.add_parser("stage-release", help="Stage an immutable versioned runtime.")
    stage.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    stage.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[3]))
    stage.add_argument("--releases-root", default=str(DEFAULT_RELEASES_ROOT))
    stage.add_argument("--python")
    stage.add_argument("--apply", action="store_true")
    stage.add_argument("--proposal-sha256")

    rollback = sub.add_parser("rollback", help="Prepare or apply the rollback slot.")
    rollback.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    rollback.add_argument("--launch-agent-path")
    rollback.add_argument("--rollback-path")
    rollback.add_argument("--apply", action="store_true")
    rollback.add_argument("--proposal-sha256")
    rollback.add_argument("--no-activate", action="store_true")
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "init":
        root = str(Path(args.project_root).expanduser().resolve(strict=True))
        current = load_policy(root)
        if current.exists:
            return current.public()
        proposal = prepare_policy_update(root, patch={}, expected_revision=0)
        if not args.apply:
            return proposal
        if args.proposal_sha256 != proposal["proposal_sha256"]:
            raise HubV2Error(
                "proposal_digest_required",
                "Pass the proposal_sha256 from the reviewed init plan.",
                scope="cli",
            )
        return apply_policy_update(
            root,
            proposal=proposal,
            proposal_sha256=proposal["proposal_sha256"],
        ).public()
    if args.command == "setup":
        proposal = plan_setup(
            args.repo_root,
            target_root=args.target_root,
            launch_agents_dir=args.launch_agents_dir,
            state_root=args.state_root,
            runtime_root=args.runtime_root,
        )
        if not args.apply:
            public = dict(proposal)
            public.pop("_host_plan", None)
            return public
        if args.proposal_sha256 != proposal["proposal_sha256"]:
            raise HubV2Error(
                "proposal_digest_required",
                "Pass the proposal_sha256 from the reviewed setup plan.",
                scope="cli",
            )
        return apply_setup(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=not args.no_activate,
        )
    if args.command == "doctor":
        client = HubDaemonClient(args.socket)
        return client.request(
            "tools/call",
            {
                "name": "agent_hub_doctor",
                "arguments": {
                    "project_root": str(Path(args.project_root).expanduser().resolve(strict=True)),
                    "live": args.live,
                    "repair": "prepare" if args.repair else "none",
                },
            },
        )
    if args.command == "repair":
        proposal = plan_repair(args.state_db)
        if not args.apply:
            return proposal
        if args.proposal_sha256 != proposal["proposal_sha256"]:
            raise HubV2Error(
                "proposal_digest_required",
                "Pass the proposal_sha256 from the reviewed repair plan.",
                scope="cli",
            )
        return apply_repair(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
        )
    if args.command == "index":
        return index_project(
            HubStore(args.state_db),
            project_root=str(Path(args.project_root).expanduser().resolve(strict=True)),
        )
    if args.command == "update":
        proposal = plan_update(
            args.candidate_root,
            launch_agent_path=args.launch_agent_path,
            rollback_path=args.rollback_path or ROLLBACK_PLIST,
        )
        if not args.apply:
            public = dict(proposal)
            public.pop("_before", None)
            public.pop("_after", None)
            return public
        if args.proposal_sha256 != proposal["proposal_sha256"]:
            raise HubV2Error(
                "proposal_digest_required",
                "Pass the proposal_sha256 from the reviewed update plan.",
                scope="cli",
            )
        return apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=not args.no_activate,
            rollback_path=args.rollback_path or ROLLBACK_PLIST,
        )
    if args.command == "stage-release":
        kwargs = {
            "releases_root": args.releases_root,
        }
        if args.python:
            kwargs["python_executable"] = args.python
        proposal = plan_stage(args.repo_root, **kwargs)
        if not args.apply:
            return proposal
        if args.proposal_sha256 != proposal["proposal_sha256"]:
            raise HubV2Error(
                "proposal_digest_required",
                "Pass the proposal_sha256 from the reviewed staging plan.",
                scope="cli",
            )
        return apply_stage(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
        )
    if args.command == "rollback":
        kwargs: dict[str, Any] = {"launch_agent_path": args.launch_agent_path}
        if args.rollback_path:
            kwargs["rollback_path"] = args.rollback_path
        proposal = plan_rollback(**kwargs)
        if not args.apply:
            public = dict(proposal)
            public.pop("_before", None)
            public.pop("_after", None)
            return public
        if args.proposal_sha256 != proposal["proposal_sha256"]:
            raise HubV2Error(
                "proposal_digest_required",
                "Pass the proposal_sha256 from the reviewed rollback plan.",
                scope="cli",
            )
        apply_kwargs: dict[str, Any] = {}
        if args.rollback_path:
            apply_kwargs["rollback_path"] = args.rollback_path
        return apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=not args.no_activate,
            **apply_kwargs,
        )
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
    except HubV2Error as exc:
        result = {"success": False, "error": exc.public()}
        code = 2
    except OSError:
        result = {
            "success": False,
            "error": {
                "code": "local_io_error",
                "message": "Agent Hub could not access a required local path.",
                "scope": "cli",
                "retryable": False,
                "safe_details": {},
            },
        }
        code = 2
    else:
        result.setdefault("success", True)
        code = 0
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
