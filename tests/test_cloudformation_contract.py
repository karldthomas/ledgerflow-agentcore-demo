from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from cloudformation.deploy import build_plan
from cloudformation.manifest import load_manifest
from cloudformation.scripts.validate_stack_contract import validate
from cloudformation.scripts.replace_environment_vars import resolve_configuration


ROOT = Path(__file__).parents[1]
STACKS = ROOT / "cloudformation" / "stacks"
STACK_ORDER = load_manifest().stack_ids


def test_matching_stack_json_files_and_forbidden_coupling_are_absent():
    templates = list(STACKS.glob("*.yaml"))
    assert {path.stem for path in templates} == set(STACK_ORDER)
    for template in templates:
        config = template.with_suffix(".json")
        assert config.exists()
        text = template.read_text(encoding="utf-8")
        assert "Fn::ImportValue" not in text
        assert "AWS::CloudFormation::Stack" not in text
        assert "Export:" not in text


def test_configuration_precedence_is_overrides_context_environment_then_default(monkeypatch):
    configuration = {
        "Configuration": [
            {"Key": "EnvironmentName", "Value": "${ENVIRONMENT_NAME}", "Default": "fallback"},
            {"Key": "KmsKeyArn", "Value": "${CF_OUT_KmsKeyArn}"},
        ]
    }
    monkeypatch.setenv("ENVIRONMENT_NAME", "from-env")
    resolved = resolve_configuration(
        configuration,
        context={"CF_OUT_KmsKeyArn": "from-context", "ENVIRONMENT_NAME": "from-context-env"},
        overrides={"ENVIRONMENT_NAME": "from-override"},
    )
    values = {entry["Key"]: entry["Value"] for entry in resolved["Configuration"]}
    assert values == {"EnvironmentName": "from-override", "KmsKeyArn": "from-context"}


def test_plan_resolves_first_stack_without_aws_and_leaves_later_outputs_symbolic():
    plan, _ = build_plan(
        "00-security",
        environment="test",
        context={},
        overrides={},
        allow_unresolved=False,
        stacks_dir=STACKS,
    )
    assert plan.stack_name == "ledgerflow-test-00-security"
    assert plan.parameters["EnvironmentName"] == "test"

    later, _ = build_plan(
        "01-storage",
        environment="test",
        context={},
        overrides={},
        allow_unresolved=True,
        stacks_dir=STACKS,
    )
    assert "${CF_OUT_KmsKeyArn}" in later.unresolved


def test_full_plan_is_available_without_aws_credentials():
    result = subprocess.run(
        [sys.executable, "cloudformation/deploy.py", "--environment", "test", "--plan"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert [stack["stack_id"] for stack in payload["stacks"]] == list(STACK_ORDER)
    assert payload["stacks"][-1]["unresolved"]


def test_s3_vectors_index_name_is_explicit_and_runtime_model_switch_is_optional():
    vector_template = (STACKS / "02-s3-vectors.yaml").read_text(encoding="utf-8")
    runtime_template = (STACKS / "04-agentcore.yaml").read_text(encoding="utf-8")
    assert "IndexName: !Ref VectorIndexName" in vector_template
    assert "VectorBucketName: !Ref EvidenceVectorBucket" not in vector_template
    assert "Value: !Sub ledgerflow-${EnvironmentName}-vectors" in vector_template
    assert "ReasoningModelId" in runtime_template
    assert "MODEL_REASONING_ENABLED" in runtime_template


def test_general_purpose_buckets_use_account_regional_namespaces():
    foundation = (STACKS / "00-security.yaml").read_text(encoding="utf-8")
    storage = (STACKS / "01-storage.yaml").read_text(encoding="utf-8")
    assert "BucketNamePrefix: !Sub ledgerflow-${EnvironmentName}-artifacts" in foundation
    assert "BucketNamePrefix: !Sub ledgerflow-${EnvironmentName}-raw" in storage
    assert "BucketNamePrefix: !Sub ledgerflow-${EnvironmentName}-reports" in storage
    assert foundation.count("BucketNamespace: account-regional") == 1
    assert storage.count("BucketNamespace: account-regional") == 2


def test_stack_configuration_covers_all_required_template_parameters():
    for template in STACKS.glob("*.yaml"):
        assert validate(template, template.with_suffix(".json")) == []


def test_direct_cloudformation_writes_are_rejected_in_favor_of_sam():
    result = subprocess.run(
        [
            sys.executable,
            "cloudformation/deploy.py",
            "--environment",
            "test",
            "--stack",
            "00-security",
            "--execute",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "cloudformation/sam_deploy.py" in result.stderr
