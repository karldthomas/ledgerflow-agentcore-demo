from __future__ import annotations

from pathlib import Path

import pytest

from cloudformation.manifest import load_manifest


ROOT = Path(__file__).parents[1]


def test_manifest_owns_order_dependencies_names_and_derived_configs():
    manifest = load_manifest(ROOT / "cloudformation" / "deployment.yaml")

    assert manifest.stack_ids == (
        "00-security",
        "01-storage",
        "02-s3-vectors",
        "03-tools",
        "04-agentcore",
        "05-workflow",
        "06-observability",
        "07-web-app",
    )
    assert manifest.definition("07-web-app").config_path.name == "07-web-app.json"
    assert manifest.stack_name("test", "01-storage") == "ledgerflow-test-01-storage"
    assert manifest.predecessors("03-tools") == ("00-security", "01-storage", "02-s3-vectors")


def test_manifest_places_lifecycle_steps_at_stack_boundaries():
    manifest = load_manifest()

    assert [step.handler for step in manifest.steps_before("00-security")] == [
        "run_tests",
        "validate_contracts",
        "check_aws_identity",
    ]
    assert [step.handler for step in manifest.steps_after("00-security", {"00-security"})] == [
        "build_runtime_image",
    ]
    assert [step.handler for step in manifest.steps_after("02-s3-vectors", {"00-security", "01-storage", "02-s3-vectors"})] == [
        "ingest_fixtures",
    ]


def test_manifest_rejects_dependency_cycles(tmp_path):
    stacks = tmp_path / "stacks"
    stacks.mkdir()
    for stack_id in ("a", "b"):
        (stacks / f"{stack_id}.yaml").write_text("AWSTemplateFormatVersion: '2010-09-09'\n", encoding="utf-8")
        (stacks / f"{stack_id}.json").write_text('{"Configuration": []}\n', encoding="utf-8")
    manifest_path = tmp_path / "deployment.yaml"
    manifest_path.write_text(
        """version: 1
name: example
stack_name_pattern: \"{name}-{environment}-{id}\"
stacks:
  - id: a
    template: stacks/a.yaml
    depends_on: [b]
  - id: b
    template: stacks/b.yaml
    depends_on: [a]
steps: []
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dependency cycle"):
        load_manifest(manifest_path)
