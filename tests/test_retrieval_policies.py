from __future__ import annotations

import json

from ledgerflow.cloud_agent import CloudInvestigationHandler
from ledgerflow.planner import BedrockInvestigationPlanner
from ledgerflow.retrieval_policies import classify_task, policy_for


class _PlannerClient:
    def __init__(self, task_type: str):
        self.task_type = task_type
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "task_type": self.task_type,
                                    "goal": "Check the reported payment behavior.",
                                    "incident_query": "INC-1042",
                                    "evidence_query": "duplicate invoice timeout",
                                    "deployment_version": "3.8.2",
                                    "rationale": "Use the task-specific evidence policy.",
                                }
                            )
                        }
                    ]
                }
            }
        }


def test_task_classifier_has_safe_fallbacks():
    assert classify_task("Explain why retries need an idempotency key") == "explanation"
    assert classify_task("Verify whether the invoice was settled") == "verification"
    assert classify_task("Investigate the duplicate invoice") == "investigation"
    assert classify_task("anything", "explanation") == "explanation"
    assert classify_task("anything", "untrusted-task") == "investigation"


def test_policies_bound_sources_and_limits():
    explanation = policy_for("explanation")
    verification = policy_for("verification")
    investigation = policy_for("investigation")

    assert explanation.policy_id == "explanation-v1"
    assert explanation.include_invoices is False
    assert explanation.include_logs is False
    assert explanation.vector_source_types == ("runbook",)
    assert verification.strategy == "authoritative"
    assert verification.include_invoices is True
    assert investigation.include_memory is True


def test_planner_returns_task_type_and_policy_from_model_output():
    client = _PlannerClient("verification")
    planner = BedrockInvestigationPlanner(
        model_id="test-model",
        client=client,
        prompt_default_arn="arn:aws:bedrock:us-east-1:123456789012:prompt/PLANNER:1",
    )

    plan = planner.plan(
        {
            "customer_id": "ACME-482",
            "incident_id": "INC-1042",
            "request": "Verify the duplicate invoice.",
        }
    )

    assert plan.task_type == "verification"
    assert plan.retrieval_policy_id == "verification-v1"
    assert plan.include_invoices is True
    assert plan.to_dict()["task_type"] == "verification"
    prompt_input = json.loads(client.calls[0]["promptVariables"]["input"]["text"])
    assert prompt_input["allowed_output"]["task_type"]


def test_explicit_task_type_overrides_model_classification():
    client = _PlannerClient("investigation")
    planner = BedrockInvestigationPlanner(
        model_id="test-model",
        client=client,
        prompt_default_arn="arn:aws:bedrock:us-east-1:123456789012:prompt/PLANNER:1",
    )

    plan = planner.plan(
        {
            "customer_id": "ACME-482",
            "request": "Explain the retry behavior.",
            "task_type": "explanation",
        }
    )

    assert plan.task_type == "explanation"
    assert plan.retrieval_policy_id == "explanation-v1"
    assert plan.include_invoices is False
    assert plan.include_logs is False

def test_model_disabled_default_plan_uses_task_policy():
    plan = CloudInvestigationHandler._default_plan(
        {
            "customer_id": "ACME-482",
            "request": "Explain why the retry key matters.",
        }
    )

    assert plan.task_type == "explanation"
    assert plan.retrieval_policy_id == "explanation-v1"
    assert plan.include_invoices is False
    assert plan.include_logs is False
