"""Isolated provider worker protocol.

Each process serves newline-delimited JSON requests on stdin/stdout. The daemon
never imports provider credentials or provider HTTP modules itself.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from agent_hub import operations

from .contracts import ensure_public_model_id, require_object, validate_task
from .errors import HubV2Error, public_failure, safe_unexpected_error
from .provider_manifests import manifest_for

WORKER_PROTOCOL = "agent_hub_provider_worker_v2"
MAX_REQUEST_CHARS = 4_000_000


def _payload(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    raise HubV2Error(
        "provider_protocol_error",
        "The provider returned an invalid response.",
        scope="provider",
    )


def _status(provider: str, params: Mapping[str, Any]) -> dict[str, Any]:
    return _payload(
        operations.dispatch_tool(
            "agent_hub_status",
            {"provider": provider, "probe": bool(params.get("probe", False))},
        )
    )


def _catalog(provider: str, params: Mapping[str, Any]) -> dict[str, Any]:
    arguments: dict[str, Any] = {"provider": provider}
    if params.get("refresh") is True:
        arguments["refresh"] = True
    result = _payload(operations.dispatch_tool("agent_hub_list_models", arguments))
    models = (
        result.get("data", {}).get("models", {}).get(provider, {}).get("models", [])
        if isinstance(result.get("data"), Mapping)
        else []
    )
    if isinstance(models, list):
        for model in models:
            if isinstance(model, Mapping) and isinstance(model.get("id"), str):
                ensure_public_model_id(model["id"])
    return result


def _invoke_arguments(
    provider: str,
    task: Mapping[str, Any],
    *,
    model: str | None = None,
) -> tuple[str, dict[str, Any]]:
    capability = task["capability"]
    inline = str(task.get("inline_input") or "")
    intent = str(task["intent"])
    constraints = task.get("constraints") or {}
    common: dict[str, Any] = {"provider": provider}
    if model:
        common["model"] = ensure_public_model_id(model)
    if constraints.get("max_tokens") is not None:
        common["max_tokens"] = constraints["max_tokens"]
    if constraints.get("timeout_seconds") is not None:
        common["timeout_sec"] = constraints["timeout_seconds"]
    if capability in {"chat", "review", "decide"}:
        prompt = intent if not inline else f"{intent}\n\n{inline}"
        return "agent_hub_chat", {**common, "prompt": prompt}
    if capability == "search":
        query = inline or intent
        return "agent_hub_search", {**common, "query": query}
    if capability == "write":
        return "agent_hub_write", {
            **common,
            "instruction": intent,
            "source_text": inline,
            # v2 accounts for review and revision as explicit DAG steps. Hidden
            # v1 rewrites would bypass the run's leaf-call and time budgets.
            "quality_rewrite_attempts": 0,
        }
    if capability == "image":
        return "agent_hub_generate_image", {**common, "prompt": inline or intent}
    raise HubV2Error(
        "unsupported_worker_capability",
        "This capability must be handled by the local runtime.",
        scope="provider",
        safe_details={"capability": capability},
    )


def _invoke(provider: str, params: Mapping[str, Any]) -> dict[str, Any]:
    task = validate_task(params.get("task"))
    model = params.get("model")
    tool, arguments = _invoke_arguments(
        provider,
        task,
        model=str(params.get("model") or "") or None,
    )
    if model is not None:
        arguments["model"] = ensure_public_model_id(model)
    return _payload(operations.dispatch_tool(tool, arguments))


def _plan(provider: str, params: Mapping[str, Any]) -> dict[str, Any]:
    task = validate_task(params.get("task"))
    project_root = params.get("project_root")
    if not isinstance(project_root, str):
        raise HubV2Error(
            "invalid_project_root",
            "project_root is required for planner execution.",
            scope="planner",
        )
    prompt = str(params.get("planner_prompt") or task["intent"])
    model = params.get("model")
    arguments: dict[str, Any] = {
        "workflow_id": "adaptive",
        "prompt": prompt,
        "project_root": project_root,
        "policy_mode": "required",
        "handoff_mode": "off",
        "planner_provider": provider,
    }
    constraints = task.get("constraints")
    if isinstance(constraints, Mapping):
        if constraints.get("max_leaf_calls") is not None:
            arguments["max_leaf_calls"] = int(constraints["max_leaf_calls"])
        if constraints.get("max_tokens") is not None:
            arguments["planner_max_tokens"] = int(constraints["max_tokens"])
        if constraints.get("timeout_seconds") is not None:
            timeout = float(constraints["timeout_seconds"])
            arguments["workflow_timeout"] = timeout
            arguments["per_call_timeout"] = timeout
    if model is not None:
        arguments["planner_model"] = ensure_public_model_id(model)
    result = _payload(operations.dispatch_tool("agent_hub_plan_workflow", arguments))
    if result.get("success") is False:
        error = result.get("error")
        reason = ""
        if isinstance(error, Mapping):
            candidate = str(error.get("type") or error.get("code") or "")
            if candidate.replace("_", "").replace("-", "").isalnum():
                reason = candidate[:64]
        raise HubV2Error(
            "planner_execution_failed",
            "The planner could not produce a valid plan.",
            scope="planner",
            retryable=True,
            safe_details={"reason_code": reason or "planner_failed"},
        )
    return result


def handle_request(provider: str, request: Mapping[str, Any]) -> dict[str, Any]:
    request_id = request.get("id")
    method = str(request.get("method") or "")
    try:
        params = require_object(request.get("params", {}), field="params")
        if method == "initialize":
            data: dict[str, Any] = {
                "protocol": WORKER_PROTOCOL,
                "manifest": manifest_for(provider),
            }
        elif method == "status":
            data = _status(provider, params)
        elif method == "catalog":
            data = _catalog(provider, params)
        elif method == "invoke":
            data = _invoke(provider, params)
        elif method == "plan":
            data = _plan(provider, params)
        elif method == "cancel":
            data = {
                "success": True,
                "operation": "cancel",
                "data": {"cancelled": False, "reason": "request_process_scoped"},
            }
        elif method == "shutdown":
            data = {"success": True, "operation": "shutdown", "data": {}}
        else:
            raise HubV2Error(
                "unknown_worker_method",
                "The provider worker method is not supported.",
                scope="provider",
            )
        return {"id": request_id, "success": True, "result": data}
    except HubV2Error as exc:
        return {
            "id": request_id,
            **public_failure(exc, operation=method or "provider_worker"),
        }
    except Exception:  # noqa: BLE001
        return {
            "id": request_id,
            **safe_unexpected_error(
                operation=method or "provider_worker",
                scope="provider",
            ),
        }


def serve(provider: str) -> int:
    manifest_for(provider)
    for raw in sys.stdin:
        should_shutdown = False
        if len(raw) > MAX_REQUEST_CHARS:
            response = {
                "id": None,
                **public_failure(
                    HubV2Error(
                        "request_too_large",
                        "The provider worker request is too large.",
                        scope="provider",
                    ),
                    operation="provider_worker",
                ),
            }
        else:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, Mapping):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                response = {
                    "id": None,
                    **public_failure(
                        HubV2Error(
                            "invalid_request",
                            "The provider worker request is invalid.",
                            scope="provider",
                        ),
                        operation="provider_worker",
                    ),
                }
            else:
                response = handle_request(provider, parsed)
                should_shutdown = parsed.get("method") == "shutdown"
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if should_shutdown:
            break
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: python -m agent_hub.v2.provider_worker <provider>", file=sys.stderr)
        return 2
    try:
        return serve(arguments[0])
    except KeyError:
        print("unknown provider", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
