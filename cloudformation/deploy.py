#!/usr/bin/env python3
"""Deploy independent LedgerFlow CloudFormation stacks with a JSON context.

This deliberately does not use CloudFormation exports/imports. Each stack's
ordinary Outputs are read with ``describe-stacks`` and mapped to
``CF_OUT_<OutputKey>`` values for the next matching JSON configuration file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .scripts.config_to_parameters import to_parameters
    from .scripts.replace_environment_vars import (
        UnresolvedVariable,
        configuration_values,
        load_configuration,
        resolve_configuration,
    )
    from .scripts.validate_stack_contract import validate
except ImportError:  # Running as ``python cloudformation/deploy.py``.
    from scripts.config_to_parameters import to_parameters
    from scripts.replace_environment_vars import UnresolvedVariable, configuration_values, load_configuration, resolve_configuration
    from scripts.validate_stack_contract import validate


STACK_ORDER = (
    "00-security",
    "01-storage",
    "02-s3-vectors",
    "03-tools",
    "04-agentcore",
    "05-workflow",
    "06-observability",
    "07-web-app",
)
PLACEHOLDER = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


@dataclass(frozen=True)
class StackPlan:
    stack_id: str
    stack_name: str
    template_path: str
    config_path: str
    parameters: dict[str, str]
    unresolved: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stack_id": self.stack_id,
            "stack_name": self.stack_name,
            "template_path": self.template_path,
            "config_path": self.config_path,
            "parameters": self.parameters,
            "unresolved": self.unresolved,
        }


def parse_overrides(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--set requires KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"--set key cannot be empty: {item!r}")
        result[key] = value
    return result


def load_context(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"deployment context must be an object: {path}")
    return {str(key): str(value) for key, value in payload.items()}


def save_context(path: Path, context: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(context.items())), indent=2) + "\n", encoding="utf-8")


def stack_name(environment: str, stack_id: str) -> str:
    return f"ledgerflow-{environment}-{stack_id}"


def stack_ids(selected: str | None, start: str | None) -> list[str]:
    if selected:
        if selected not in STACK_ORDER:
            raise ValueError(f"unknown stack {selected}; choose from {', '.join(STACK_ORDER)}")
        return [selected]
    if start:
        if start not in STACK_ORDER:
            raise ValueError(f"unknown start stack {start}; choose from {', '.join(STACK_ORDER)}")
        return list(STACK_ORDER[STACK_ORDER.index(start) :])
    return list(STACK_ORDER)


def build_plan(
    stack_id: str,
    *,
    environment: str,
    context: dict[str, str],
    overrides: dict[str, str],
    allow_unresolved: bool,
    stacks_dir: Path,
) -> tuple[StackPlan, dict[str, str]]:
    template_path = stacks_dir / f"{stack_id}.yaml"
    config_path = stacks_dir / f"{stack_id}.json"
    if not template_path.exists() or not config_path.exists():
        raise FileNotFoundError(f"missing matching stack files for {stack_id}: {template_path}, {config_path}")
    errors = validate(template_path, config_path)
    if errors:
        raise ValueError("; ".join(errors))
    raw_config = load_configuration(config_path)
    merged_overrides = {"EnvironmentName": environment, "ENVIRONMENT_NAME": environment, **overrides}
    try:
        resolved = resolve_configuration(
            raw_config,
            context=context,
            overrides=merged_overrides,
            allow_unresolved=allow_unresolved,
        )
    except UnresolvedVariable:
        raise
    values = configuration_values(resolved)
    unresolved = sorted({match.group(0) for value in values.values() for match in PLACEHOLDER.finditer(value)})
    plan = StackPlan(
        stack_id=stack_id,
        stack_name=stack_name(environment, stack_id),
        template_path=str(template_path),
        config_path=str(config_path),
        parameters=values,
        unresolved=unresolved,
    )
    return plan, context


def _boto3_client(region: str):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required for --execute/--change-set; install the AWS deployment extras") from exc
    return boto3.client("cloudformation", region_name=region)


def _stack_exists(client: Any, name: str) -> bool:
    try:
        client.describe_stacks(StackName=name)
        return True
    except Exception as exc:
        message = str(exc)
        if "does not exist" in message or "doesn't exist" in message:
            return False
        raise


def _wait_for_change_set(client: Any, stack_name_value: str, change_set_name: str) -> dict[str, Any]:
    while True:
        response = client.describe_change_set(StackName=stack_name_value, ChangeSetName=change_set_name)
        status = response.get("Status")
        if status in {"CREATE_COMPLETE", "FAILED"}:
            if status == "FAILED":
                reason = str(response.get("StatusReason", "CloudFormation change set failed"))
                if "didn't contain changes" in reason.lower() or "no updates" in reason.lower():
                    response["NoChanges"] = True
                    return response
                raise RuntimeError(reason)
            return response
        time.sleep(2)


def _wait_for_stack(client: Any, stack_name_value: str, create: bool) -> None:
    waiter_name = "stack_create_complete" if create else "stack_update_complete"
    client.get_waiter(waiter_name).wait(StackName=stack_name_value)


def deploy_stack(client: Any, plan: StackPlan, *, execute: bool, change_set_only: bool) -> dict[str, Any]:
    template_body = Path(plan.template_path).read_text(encoding="utf-8")
    exists = _stack_exists(client, plan.stack_name)
    change_set_name = f"ledgerflow-{int(time.time())}-{plan.stack_id.replace('-', '')}"
    response = client.create_change_set(
        StackName=plan.stack_name,
        ChangeSetName=change_set_name,
        ChangeSetType="UPDATE" if exists else "CREATE",
        Description=f"LedgerFlow {plan.stack_id} deployment",
        TemplateBody=template_body,
        Parameters=to_parameters({"Configuration": [{"Key": key, "Value": value} for key, value in plan.parameters.items()]}),
        Capabilities=["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
    )
    change_set = _wait_for_change_set(client, plan.stack_name, response["Id"])
    result: dict[str, Any] = {"change_set_id": response["Id"], "change_set_status": change_set.get("Status")}
    if change_set.get("NoChanges"):
        result["no_changes"] = True
        if exists:
            result["stack_status"] = client.describe_stacks(StackName=plan.stack_name)["Stacks"][0]["StackStatus"]
        return result
    if change_set_only and not execute:
        return result
    client.execute_change_set(StackName=plan.stack_name, ChangeSetName=response["Id"])
    _wait_for_stack(client, plan.stack_name, create=not exists)
    result["stack_status"] = client.describe_stacks(StackName=plan.stack_name)["Stacks"][0]["StackStatus"]
    return result


def execute_named_change_set(client: Any, *, environment: str, stack_id: str, change_set_name: str) -> dict[str, Any]:
    """Execute the exact change set returned by ``--change-set``."""

    name = stack_name(environment, stack_id)
    description = client.describe_change_set(StackName=name, ChangeSetName=change_set_name)
    if description.get("Status") == "FAILED":
        raise RuntimeError(description.get("StatusReason", "CloudFormation change set is failed"))
    client.execute_change_set(StackName=name, ChangeSetName=change_set_name)
    _wait_for_stack(client, name, create=description.get("ChangeSetType") == "CREATE")
    return {
        "change_set_id": description.get("ChangeSetId", change_set_name),
        "change_set_status": description.get("Status"),
        "stack_status": client.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"],
    }


def output_context(client: Any, name: str, context: dict[str, str]) -> dict[str, str]:
    stack = client.describe_stacks(StackName=name)["Stacks"][0]
    for output in stack.get("Outputs", []):
        context[f"CF_OUT_{output['OutputKey']}"] = str(output.get("OutputValue", ""))
    return context


def hydrate_prior_outputs(
    client: Any,
    *,
    environment: str,
    first_stack: str,
    context: dict[str, str],
) -> dict[str, str]:
    """Rebuild the prior-stack portion of context from CloudFormation.

    A deployment context file is a convenience cache, not the source of truth.
    When a caller resumes at a later stack and the cache is missing or stale,
    read the ordinary Outputs of the already-deployed predecessor stacks. This
    keeps stacks independent without introducing CloudFormation imports.
    """

    first_index = STACK_ORDER.index(first_stack)
    for predecessor in STACK_ORDER[:first_index]:
        name = stack_name(environment, predecessor)
        try:
            context = output_context(client, name, context)
        except Exception as exc:
            raise RuntimeError(
                f"cannot hydrate outputs for predecessor stack {name}; "
                "deploy it first or provide a complete --context-file"
            ) from exc
    return context


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT_NAME", "dev"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")))
    parser.add_argument("--stack", choices=STACK_ORDER)
    parser.add_argument("--from", dest="start_stack", choices=STACK_ORDER)
    parser.add_argument("--plan", action="store_true", help="resolve and validate only; never call AWS")
    parser.add_argument("--change-set", action="store_true", help="create a change set for the selected stack(s) without executing it")
    parser.add_argument("--execute", action="store_true", help="create and execute change sets in dependency order")
    parser.add_argument(
        "--execute-change-set",
        metavar="STACK_ID=CHANGE_SET",
        help="execute the exact reviewed change set returned by --change-set",
    )
    parser.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--allow-unresolved", action="store_true", help="leave CF_OUT placeholders in a local plan")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    selected_modes = sum(bool(value) for value in (args.plan, args.execute, args.change_set, args.execute_change_set))
    if selected_modes > 1:
        raise SystemExit("choose exactly one of --plan, --change-set, --execute, or --execute-change-set")
    if selected_modes == 0:
        raise SystemExit("choose --plan, --change-set, --execute, or --execute-change-set; AWS writes are never implicit")
    if args.execute_change_set and "=" not in args.execute_change_set:
        raise SystemExit("--execute-change-set requires STACK_ID=CHANGE_SET")
    overrides = parse_overrides(args.overrides)
    context_path = args.context_file or Path(__file__).parent / "build" / f"deployment-context-{args.environment}.json"
    context = load_context(context_path)
    context.update({"EnvironmentName": args.environment, "ENVIRONMENT_NAME": args.environment})
    stacks_dir = Path(__file__).parent / "stacks"
    plans: list[dict[str, Any]] = []
    if args.execute_change_set:
        client = _boto3_client(args.region)
        stack_id, change_set_name = args.execute_change_set.split("=", 1)
        if stack_id not in STACK_ORDER:
            raise SystemExit(f"unknown stack {stack_id}; choose from {', '.join(STACK_ORDER)}")
        if args.stack and args.stack != stack_id:
            raise SystemExit("--stack must match the stack ID in --execute-change-set")
        result = execute_named_change_set(
            client,
            environment=args.environment,
            stack_id=stack_id,
            change_set_name=change_set_name,
        )
        context = output_context(client, stack_name(args.environment, stack_id), context)
        save_context(context_path, context)
        print(json.dumps({"environment": args.environment, "region": args.region, "context_file": str(context_path), "stack": stack_id, "deployment": result}, indent=2, sort_keys=True))
        return 0
    selected_stack_ids = stack_ids(args.stack, args.start_stack)
    if args.change_set and len(selected_stack_ids) != 1:
        raise SystemExit("--change-set reviews one stack at a time; pass --stack STACK_ID and execute it before reviewing the next stack")
    client = None if args.plan else _boto3_client(args.region)
    if client is not None:
        context = hydrate_prior_outputs(
            client,
            environment=args.environment,
            first_stack=selected_stack_ids[0],
            context=context,
        )
    for stack_id in selected_stack_ids:
        plan, context = build_plan(
            stack_id,
            environment=args.environment,
            context=context,
            overrides=overrides,
            allow_unresolved=args.allow_unresolved or args.plan,
            stacks_dir=stacks_dir,
        )
        if plan.unresolved and not args.plan:
            raise SystemExit(f"unresolved values for {stack_id}: {', '.join(plan.unresolved)}")
        item = plan.to_dict()
        if client is not None:
            item["deployment"] = deploy_stack(client, plan, execute=args.execute, change_set_only=args.change_set)
            if args.execute:
                context = output_context(client, plan.stack_name, context)
                save_context(context_path, context)
        plans.append(item)
    if args.plan:
        print(json.dumps({"environment": args.environment, "region": args.region, "context_file": str(context_path), "stacks": plans}, indent=2, sort_keys=True))
    else:
        save_context(context_path, context)
        print(json.dumps({"environment": args.environment, "region": args.region, "context_file": str(context_path), "stacks": plans}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
