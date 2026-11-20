from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cloudformation.deploy import STACK_ORDER
from cloudformation.manifest import load_manifest
from cloudformation.sam_deploy import LAMBDA_STACKS, _stage_source, _stage_template, build_parser


ROOT = Path(__file__).parents[1]


def test_sam_adapter_accepts_every_manifest_stack():
    parser = build_parser()
    manifest = load_manifest()
    assert tuple(STACK_ORDER) == manifest.stack_ids
    for stack_id in manifest.stack_ids:
        parsed = parser.parse_args(["--stack", stack_id])
        assert parsed.stack == stack_id
    assert LAMBDA_STACKS == {"02-s3-vectors", "03-tools", "05-workflow", "07-web-app"}


def test_lambda_stacks_use_sam_transform_and_no_manual_artifact_parameters():
    for stack_id in ("02-s3-vectors", "03-tools", "05-workflow", "07-web-app"):
        template = (ROOT / "cloudformation" / "stacks" / f"{stack_id}.yaml").read_text(encoding="utf-8")
        config = (ROOT / "cloudformation" / "stacks" / f"{stack_id}.json").read_text(encoding="utf-8")
        assert "Transform: AWS::Serverless-2016-10-31" in template
        assert "ArtifactBucketName" not in template
        assert "LambdaPackageKey" not in template
        assert "WebArtifactKey" not in template
        assert "ArtifactBucketName" not in config
        assert "LambdaPackageKey" not in config
        assert "WebArtifactKey" not in config


def test_sam_staging_contains_only_function_source(tmp_path):
    expected = {
        "02-s3-vectors": ("enhanced_index_handler.py", "requirements.txt"),
        "03-tools": ("tool_handler.py", "requirements.txt"),
        "05-workflow": ("lambda/audit_handler.py", "ledgerflow/audit.py"),
        "07-web-app": ("lambda/web_app.py", "frontend/app.js"),
    }
    for stack_id, expected_files in expected.items():
        stage = tmp_path / stack_id
        source, original_code_uri = _stage_source(stack_id, stage)
        staged_template = _stage_template(
            SimpleNamespace(template_path=str(ROOT / "cloudformation" / "stacks" / f"{stack_id}.yaml")),
            stage,
            source,
            original_code_uri,
        )
        assert all((source / path).exists() for path in expected_files)
        assert not (source / ".ledgerflow").exists()
        assert "CodeUri: " + original_code_uri not in staged_template.read_text(encoding="utf-8")
