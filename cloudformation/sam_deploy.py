#!/usr/bin/env python3
"""Deploy one LedgerFlow stack through AWS SAM.

The repository keeps its CloudFormation stacks independent so their ordinary
outputs can be passed through the deployment context. SAM owns the deployment
change set and progress reporting for every stack. SAM builds every template;
for stacks that contain functions it also owns source build, dependency
installation, packaging, and upload.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from .manifest import DEFAULT_MANIFEST_PATH, load_manifest
    from .deploy import (
        _boto3_client,
        build_plan,
        hydrate_prior_outputs,
        load_context,
        output_context,
        parse_overrides,
        save_context,
        StackPlan,
    )
except ImportError:  # Running as ``python cloudformation/sam_deploy.py``.
    from manifest import DEFAULT_MANIFEST_PATH, load_manifest
    from deploy import (
        StackPlan,
        _boto3_client,
        build_plan,
        hydrate_prior_outputs,
        load_context,
        output_context,
        parse_overrides,
        save_context,
    )


LAMBDA_STACKS = frozenset(
    {
        "02-s3-vectors",
        "03-tools",
        "05-workflow",
        "07-web-app",
    }
)
ROOT = Path(__file__).resolve().parent.parent
SAM_TMP_ROOT = Path(tempfile.gettempdir()) / "ledgerflow-sam"


def _run(command: list[str]) -> None:
    print("+ " + " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def _build_dir(value: Path, stack_id: str) -> Path:
    directory = value if value.is_absolute() else ROOT / value
    return directory / stack_id


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _stage_source(stack_id: str, directory: Path) -> tuple[Path, str]:
    """Stage only the Lambda source needed by a SAM stack.

    Some functions use repository-root code paths in the human-readable
    templates because they need both a handler and adjacent shared assets.
    The deployment wrapper stages those assets into a temporary source tree so
    SAM never packages the local database, tests, documentation, or build
    output by accident.
    """

    source = directory / "source"
    source.mkdir(parents=True, exist_ok=True)
    lambda_root = ROOT / "lambda"
    if stack_id == "02-s3-vectors":
        _copy_file(ROOT / "cloudformation" / "enhanced_index_handler.py", source / "enhanced_index_handler.py")
        _copy_file(ROOT / "cloudformation" / "requirements.txt", source / "requirements.txt")
        return source, "../../cloudformation"
    if stack_id == "03-tools":
        _copy_file(lambda_root / "tool_handler.py", source / "tool_handler.py")
        _copy_file(lambda_root / "requirements.txt", source / "requirements.txt")
        return source, "../../lambda"
    if stack_id == "05-workflow":
        _copy_file(lambda_root / "__init__.py", source / "lambda" / "__init__.py")
        _copy_file(lambda_root / "audit_handler.py", source / "lambda" / "audit_handler.py")
        _copy_file(lambda_root / "requirements.txt", source / "requirements.txt")
        for name in ("__init__.py", "audit.py", "retrieval_policies.py"):
            _copy_file(ROOT / "ledgerflow" / name, source / "ledgerflow" / name)
        return source, "../../"
    if stack_id == "07-web-app":
        _copy_file(lambda_root / "__init__.py", source / "lambda" / "__init__.py")
        _copy_file(lambda_root / "web_app.py", source / "lambda" / "web_app.py")
        _copy_file(lambda_root / "bootstrap_admin.py", source / "lambda" / "bootstrap_admin.py")
        _copy_file(lambda_root / "requirements.txt", source / "requirements.txt")
        shutil.copytree(ROOT / "frontend", source / "frontend")
        return source, "../../"
    raise ValueError(f"unsupported SAM stack: {stack_id}")


def _stage_template(plan: StackPlan, directory: Path, source: Path, original_code_uri: str) -> Path:
    template_path = Path(plan.template_path)
    template = template_path.read_text(encoding="utf-8")
    marker = f"CodeUri: {original_code_uri}"
    replacement = f"CodeUri: {source}"
    if marker not in template:
        raise RuntimeError(f"could not find {marker!r} in {template_path}")
    staged = directory / "template.yaml"
    staged.write_text(template.replace(marker, replacement), encoding="utf-8")
    return staged


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT_NAME", "dev"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--stack", required=True)
    parser.add_argument(
        "--s3-bucket",
        help="SAM staging bucket, normally the Stack 00 artifact bucket; optional for small infrastructure-only templates",
    )
    parser.add_argument("--s3-prefix", default=os.environ.get("SAM_S3_PREFIX", "ledgerflow-sam"))
    parser.add_argument(
        "--resolve-s3",
        action="store_true",
        help="ask SAM to create or resolve its managed staging bucket when --s3-bucket is omitted",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=SAM_TMP_ROOT / "build",
        help="parent directory for SAM build output",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=SAM_TMP_ROOT / "cache",
        help="directory for SAM dependency/build cache",
    )
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--sam-command", default=os.environ.get("SAM_COMMAND", "sam"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        manifest.definition(args.stack)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.stack in LAMBDA_STACKS and not args.s3_bucket and not args.resolve_s3:
        raise SystemExit(f"--s3-bucket or --resolve-s3 is required for Lambda stack {args.stack}")
    if args.s3_bucket and args.resolve_s3:
        raise SystemExit("--s3-bucket and --resolve-s3 cannot be used together")
    context_path = args.context_file or ROOT / "cloudformation" / "build" / f"deployment-context-{args.environment}.json"
    context = load_context(context_path)
    context.update({"EnvironmentName": args.environment, "ENVIRONMENT_NAME": args.environment})

    client = _boto3_client(args.region)
    context = hydrate_prior_outputs(
        client,
        environment=args.environment,
        first_stack=args.stack,
        context=context,
        manifest=manifest,
    )
    plan, _ = build_plan(
        args.stack,
        environment=args.environment,
        context=context,
        overrides=parse_overrides(args.overrides),
        allow_unresolved=False,
        manifest=manifest,
    )
    if plan.unresolved:
        raise SystemExit(f"unresolved values for {args.stack}: {', '.join(plan.unresolved)}")

    build_dir = _build_dir(args.build_dir, args.stack)
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"ledgerflow-sam-{args.stack}-") as staging:
        staging_dir = Path(staging)
        if args.stack in LAMBDA_STACKS:
            source_dir, original_code_uri = _stage_source(args.stack, staging_dir)
            staged_template = _stage_template(plan, staging_dir, source_dir, original_code_uri)
        else:
            staged_template = Path(plan.template_path)

        # Run the same SAM build boundary for infrastructure-only templates as
        # for Lambda templates.  This keeps the deployment path observable and
        # lets SAM validate/translate every stack before sam deploy is called.
        built_template = build_dir / "template.yaml"
        _run(
            [
                args.sam_command,
                "build",
                "--template-file",
                str(staged_template),
                "--build-dir",
                str(build_dir),
                "--cache-dir",
                str(args.cache_dir),
                "--cached",
            ]
        )
        if not built_template.exists():
            raise RuntimeError(f"SAM did not produce the translated template: {built_template}")
        template_to_deploy = built_template

        # SAM rejects the compact ``Key=`` form for an empty value.  Empty
        # optional parameters should be omitted so CloudFormation can use the
        # template default (or retain the existing stack parameter on update).
        parameter_overrides = [
            f"{key}={value}"
            for key, value in sorted(plan.parameters.items())
            if value != ""
        ]
        deploy_command = [
            args.sam_command,
            "deploy",
            "--template-file",
            str(template_to_deploy),
            "--stack-name",
            plan.stack_name,
            "--region",
            args.region,
        ]
        if args.s3_bucket:
            deploy_command.extend(
                [
                    "--s3-bucket",
                    args.s3_bucket,
                    "--s3-prefix",
                    f"{args.s3_prefix.rstrip('/')}/{args.environment}/{args.stack}",
                ]
            )
        elif args.resolve_s3:
            deploy_command.append("--resolve-s3")
        deploy_command.extend(
            [
                "--capabilities",
                "CAPABILITY_NAMED_IAM",
                "CAPABILITY_AUTO_EXPAND",
                "--no-confirm-changeset",
                "--no-fail-on-empty-changeset",
                "--parameter-overrides",
                *parameter_overrides,
            ]
        )
        _run(deploy_command)

    context = output_context(client, plan.stack_name, context)
    save_context(context_path, context)
    print(
        json.dumps(
            {
                "environment": args.environment,
                "region": args.region,
                "context_file": str(context_path),
                "stack": plan.stack_id,
                "stack_name": plan.stack_name,
                "build_dir": str(build_dir) if build_dir is not None else None,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
