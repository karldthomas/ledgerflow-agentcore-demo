#!/usr/bin/env python3
"""Run the manifest-defined LedgerFlow deployment workflow.

``deploy-all.sh`` is only a shell entry point.  This module parses the
deployment manifest, executes its allow-listed lifecycle handlers at the
declared stack boundaries, and delegates every CloudFormation stack to
``sam_deploy.py``.  AWS credentials are deliberately never selected here;
boto3, the AWS CLI, and SAM use the ambient environment.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, LifecycleStep, load_manifest
except ImportError:  # Running as ``python cloudformation/orchestrate.py``.
    from manifest import DEFAULT_MANIFEST_PATH, DeploymentManifest, LifecycleStep, load_manifest


ROOT = Path(__file__).resolve().parent.parent
SAM_DEPLOY = Path(__file__).with_name("sam_deploy.py")
VALIDATE = Path(__file__).with_name("validate.py")
INGEST = Path(__file__).with_name("ingest.py")

HANDLERS = frozenset(
    {
        "run_tests",
        "validate_contracts",
        "check_aws_identity",
        "build_runtime_image",
        "ingest_fixtures",
    }
)


def _run(command: list[str], *, env: dict[str, str], capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(command))
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def _boto3_client(service: str, region: str) -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise SystemExit(
            "boto3 is required for deployment orchestration; install it locally with: "
            "python3 -m pip install --user boto3"
        ) from exc
    return boto3.client(service, region_name=region)


class DeploymentRunner:
    """Execute the lifecycle handlers and stack deployments for one run."""

    def __init__(self, args: argparse.Namespace, manifest: DeploymentManifest) -> None:
        self.args = args
        self.manifest = manifest
        self.environment = args.environment
        self.region = args.region
        self.release_id = args.release_id or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        self.runtime_image_tag = args.runtime_image_tag or f"{self.environment}-{self.release_id}"
        self.sam_s3_prefix = args.sam_s3_prefix or f"ledgerflow-sam-{self.release_id}"
        self.context_path = args.context_file or ROOT / "cloudformation" / "build" / f"deployment-context-{self.environment}.json"
        self.context_path = self.context_path if self.context_path.is_absolute() else ROOT / self.context_path
        self.embedding_model_id = args.embedding_model_id
        self.image_platform = args.image_platform
        self.child_env = os.environ.copy()
        self.child_env.update(
            {
                "ENVIRONMENT_NAME": self.environment,
                "AWS_REGION": self.region,
                "AWS_DEFAULT_REGION": self.region,
                "VECTOR_DIMENSION": str(args.vector_dimension),
                "EMBEDDING_MODEL_ID": self.embedding_model_id,
                "SAM_S3_PREFIX": self.sam_s3_prefix,
            }
        )
        self.artifact_bucket: str | None = None
        self.runtime_image_uri: str | None = None
        self.deployed: set[str] = set()
        self.completed_steps: set[str] = set()
        # Delay SDK client construction until after the preflight lifecycle
        # steps.  Parsing the manifest and running repository validation should
        # remain useful without credentials or a reachable AWS metadata
        # endpoint.
        self.cloudformation: Any | None = None

    def validate_handlers(self) -> None:
        unknown = sorted({step.handler for step in self.manifest.steps} - HANDLERS)
        if unknown:
            raise SystemExit(
                "deployment manifest contains unsupported lifecycle handler(s): "
                + ", ".join(unknown)
            )

    def _stack_name(self, stack_id: str) -> str:
        return self.manifest.stack_name(self.environment, stack_id)

    def _cloudformation_client(self) -> Any:
        if self.cloudformation is None:
            self.cloudformation = _boto3_client("cloudformation", self.region)
        return self.cloudformation

    def _stack_output(self, stack_id: str, key: str) -> str:
        client = self._cloudformation_client()
        try:
            response = client.describe_stacks(StackName=self._stack_name(stack_id))
        except Exception as exc:
            raise RuntimeError(f"could not read outputs for stack {stack_id}") from exc
        stacks = response.get("Stacks", [])
        if not stacks:
            raise RuntimeError(f"stack {stack_id} returned no stack record")
        for output in stacks[0].get("Outputs", []):
            if output.get("OutputKey") == key and output.get("OutputValue"):
                return str(output["OutputValue"])
        raise RuntimeError(f"missing {key} output from {stack_id}")

    def run_tests(self) -> None:
        _run([sys.executable, "-m", "pytest"], env=self.child_env)

    def validate_contracts(self) -> None:
        _run(
            [sys.executable, str(VALIDATE), "--manifest", str(self.manifest.path)],
            env=self.child_env,
        )

    def check_aws_identity(self) -> None:
        _run(["aws", "--region", self.region, "sts", "get-caller-identity"], env=self.child_env)

    def _configure_docker_auth(self, repository_uri: str, *, docker_config: Path, env: dict[str, str]) -> None:
        print("+ aws --region " + self.region + " ecr get-login-password > temporary Docker auth config")
        password = subprocess.run(
            ["aws", "--region", self.region, "ecr", "get-login-password"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            check=True,
            text=True,
        )
        token = password.stdout.strip()
        if not token:
            raise RuntimeError("ECR returned an empty authorization token")
        encoded = base64.b64encode(f"AWS:{token}".encode("utf-8")).decode("ascii")
        (docker_config / "config.json").write_text(
            json.dumps({"auths": {repository_uri: {"auth": encoded}}}) + "\n",
            encoding="utf-8",
        )

    def build_runtime_image(self) -> None:
        repository_uri = self._stack_output("00-security", "RuntimeRepositoryUri")
        repository_name = repository_uri.rsplit("/", 1)[-1]
        # Use a disposable Docker config for this deployment.  A developer's
        # global credential helper may be unavailable in a managed shell; the
        # ECR token only needs to live for the login and the immediately
        # following buildx push.
        with tempfile.TemporaryDirectory(prefix="ledgerflow-docker-config-") as docker_config:
            docker_config_path = Path(docker_config)
            docker_env = {**self.child_env, "DOCKER_CONFIG": str(docker_config_path)}
            # Docker Desktop installs buildx in the user's normal CLI-plugin
            # directory.  A disposable DOCKER_CONFIG hides that directory, so
            # preserve it while still keeping credentials isolated.
            configured_docker_dir = Path(self.child_env.get("DOCKER_CONFIG", Path.home() / ".docker"))
            plugin_dir = configured_docker_dir / "cli-plugins"
            if plugin_dir.is_dir():
                (docker_config_path / "cli-plugins").symlink_to(plugin_dir, target_is_directory=True)
            self._configure_docker_auth(repository_uri, docker_config=docker_config_path, env=docker_env)
            _run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    self.image_platform,
                    "--provenance=false",
                    "--tag",
                    f"{repository_uri}:{self.runtime_image_tag}",
                    "--push",
                    str(ROOT),
                ],
                env=docker_env,
            )
        ecr = _boto3_client("ecr", self.region)
        response = ecr.describe_images(
            repositoryName=repository_name,
            imageIds=[{"imageTag": self.runtime_image_tag}],
        )
        details = response.get("imageDetails", [])
        digest = details[0].get("imageDigest") if details else None
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise RuntimeError(
                f"could not resolve the pushed image digest for {repository_uri}:{self.runtime_image_tag}"
            )
        self.runtime_image_uri = f"{repository_uri}@{digest}"
        print(f"Runtime image: {self.runtime_image_uri}")

    def ingest_fixtures(self) -> None:
        _run(
            [
                sys.executable,
                str(INGEST),
                "--context-file",
                str(self.context_path),
                "--region",
                self.region,
                "--vector-dimension",
                str(self.args.vector_dimension),
                "--embedding-model-id",
                self.embedding_model_id,
                "--upload",
            ],
            env=self.child_env,
        )

    def _overrides(self) -> list[str]:
        values: dict[str, str] = {}
        if self.runtime_image_uri:
            values["RUNTIME_IMAGE_URI"] = self.runtime_image_uri
        for key in ("BOOTSTRAP_ADMIN_EMAIL", "BOOTSTRAP_ADMIN_TENANT_ID"):
            if self.child_env.get(key):
                values[key] = self.child_env[key]
        return [item for key, value in sorted(values.items()) for item in ("--set", f"{key}={value}")]

    def deploy_stack(self, stack_id: str) -> None:
        command = [
            sys.executable,
            str(SAM_DEPLOY),
            "--manifest",
            str(self.manifest.path),
            "--environment",
            self.environment,
            "--region",
            self.region,
            "--context-file",
            str(self.context_path),
            "--stack",
            stack_id,
        ]
        if self.artifact_bucket:
            command.extend(["--s3-bucket", self.artifact_bucket, "--s3-prefix", self.sam_s3_prefix])
        if self.args.sam_command:
            command.extend(["--sam-command", self.args.sam_command])
        command.extend(self._overrides())
        _run(command, env=self.child_env)
        if stack_id == self.manifest.stack_ids[0]:
            self.artifact_bucket = self._stack_output(stack_id, "ArtifactBucketName")

    def _skip_step(self, step: LifecycleStep) -> bool:
        return (
            (step.handler == "run_tests" and self.args.skip_tests)
            or (step.handler == "validate_contracts" and self.args.skip_validation)
            or (step.handler == "ingest_fixtures" and self.args.skip_ingest)
        )

    def execute_step(self, step: LifecycleStep) -> None:
        if step.step_id in self.completed_steps:
            return
        self.completed_steps.add(step.step_id)
        if self._skip_step(step):
            print(f"Skipping lifecycle step: {step.step_id}")
            return
        print(f"\n==> Lifecycle step: {step.step_id}")
        getattr(self, step.handler)()

    def run(self) -> None:
        self.validate_handlers()
        for stack_id in self.manifest.stack_ids:
            for step in self.manifest.steps_before(stack_id):
                self.execute_step(step)
            print(f"\n==> Deploying stack: {stack_id}")
            self.deploy_stack(stack_id)
            self.deployed.add(stack_id)
            for step in self.manifest.steps_after(stack_id, self.deployed):
                self.execute_step(step)
        web_url = self._stack_output(self.manifest.stack_ids[-1], "WebApiUrl")
        print("\n==> Deployment complete")
        print(f"Environment: {self.environment}")
        print(f"Region: {self.region}")
        print(f"Web console: {web_url}")
        print(f"Deployment context: {self.context_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT_NAME", "dev"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")))
    parser.add_argument("--vector-dimension", type=int, default=int(os.environ.get("VECTOR_DIMENSION", "1024")))
    parser.add_argument("--embedding-model-id", default=os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"))
    parser.add_argument("--release-id", default=os.environ.get("RELEASE_ID"))
    parser.add_argument("--runtime-image-tag", default=os.environ.get("RUNTIME_IMAGE_TAG"))
    parser.add_argument("--image-platform", default=os.environ.get("IMAGE_PLATFORM", "linux/arm64"))
    parser.add_argument("--sam-s3-prefix", default=os.environ.get("SAM_S3_PREFIX"))
    parser.add_argument("--sam-command", default=os.environ.get("SAM_COMMAND", "sam"))
    context_file = os.environ.get("CONTEXT_FILE")
    parser.add_argument("--context-file", type=Path, default=Path(context_file) if context_file else None)
    parser.add_argument("--skip-tests", action="store_true", default=os.environ.get("SKIP_TESTS") == "1")
    parser.add_argument("--skip-validation", action="store_true", default=os.environ.get("SKIP_VALIDATION") == "1")
    parser.add_argument("--skip-ingest", action="store_true", default=os.environ.get("SKIP_INGEST") == "1")
    return parser


def _require_tools(args: argparse.Namespace) -> None:
    for command in ("aws", "docker", args.sam_command):
        if shutil.which(command) is None:
            raise SystemExit(f"{command} is required")
    try:
        result = subprocess.run(
            ["docker", "buildx", "version"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise SystemExit("docker buildx is required") from exc
    if result.returncode:
        raise SystemExit("docker buildx is required")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _require_tools(args)
    DeploymentRunner(args, manifest).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
