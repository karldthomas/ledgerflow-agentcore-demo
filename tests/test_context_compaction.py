from __future__ import annotations

import json

import pytest

from ledgerflow.context_compaction import (
    BedrockContextSummarizer,
    ContextCompactor,
    ContextInvariantError,
    ContextSummarizationError,
)
from ledgerflow.models import IncidentReport


def _report() -> IncidentReport:
    return IncidentReport(
        incident_id="INC-1",
        customer_id="ACME-482",
        root_cause="A retry key changed after a timeout.",
        evidence_ids=["INC-1", "LOG-1"],
        confidence=0.8,
        recommended_actions=["Reconcile provider status."],
        unresolved_questions=["Did the provider settle both attempts?"],
        evidence_claims=[
            {
                "type": "fact",
                "statement": "The incident is a duplicate invoice incident.",
                "evidence_ids": ["INC-1"],
            },
            {
                "type": "relationship",
                "statement": "The retry key changed after a timeout.",
                "evidence_ids": ["LOG-1"],
            },
        ],
    )


def _evidence(count: int = 20) -> list[dict[str, object]]:
    records: list[dict[str, object]] = [
        {
            "id": "INC-1",
            "kind": "incident",
            "data": {
                "IncidentId": "INC-1",
                "CustomerId": "ACME-482",
                "TenantId": "tenant_acme",
                "Description": "Customer received two invoices.",
            },
        },
        {
            "id": "LOG-1",
            "kind": "application_log",
            "data": {
                "LogId": "LOG-1",
                "CustomerId": "ACME-482",
                "TenantId": "tenant_acme",
                "Event": "payment_timeout",
                "PayloadJson": {"original_idempotency_key": "pay-1", "retry_idempotency_key": "pay-2"},
            },
        },
    ]
    for index in range(count):
        records.append(
            {
                "id": f"LOG-{index + 2}",
                "kind": "application_log",
                "data": {
                    "LogId": f"LOG-{index + 2}",
                    "CustomerId": "ACME-482",
                    "TenantId": "tenant_acme",
                    "Event": "heartbeat",
                    "PayloadJson": {"verbose": "x" * 900},
                },
            }
        )
    return records


def test_compactor_is_deterministic_and_preserves_required_citations():
    compactor = ContextCompactor(max_tokens=1800)
    first = compactor.compact(
        base_report=_report(),
        evidence=_evidence(),
        memories=[],
        tenant_id="tenant_acme",
        workflow_context={"workflow_id": "wf-1", "completed_steps": ["retrieval"]},
    )
    second = compactor.compact(
        base_report=_report(),
        evidence=_evidence(),
        memories=[],
        tenant_id="tenant_acme",
        workflow_context={"workflow_id": "wf-1", "completed_steps": ["retrieval"]},
    )

    assert first.digest == second.digest
    assert first.token_count <= first.budget_tokens
    assert {"INC-1", "LOG-1"}.issubset(first.citable_evidence_ids)
    assert first.omitted_evidence_count > 0
    assert first.invariants["read_only"] is True
    assert first.to_manifest()["digest"] == first.digest


def test_compactor_rejects_cross_tenant_evidence():
    with pytest.raises(ContextInvariantError, match="belongs to tenant"):
        ContextCompactor(max_tokens=1800).compact(
            base_report=_report(),
            evidence=[
                {
                    "id": "OTHER-1",
                    "kind": "incident",
                    "data": {"IncidentId": "OTHER-1", "TenantId": "tenant_other"},
                }
            ],
            memories=[],
            tenant_id="tenant_acme",
        )


class _SummarizerClient:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": json.dumps(self.payload)}]}},
        }


def test_llm_semantic_pass_adds_cited_summary_inside_the_same_envelope():
    context = ContextCompactor(max_tokens=3000, reserved_tokens=800).compact(
        base_report=_report(),
        evidence=_evidence(2),
        memories=[],
        tenant_id="tenant_acme",
    )
    client = _SummarizerClient(
        {
            "summary": "The timeout was followed by a changed retry key.",
            "summary_evidence_ids": ["LOG-1"],
            "observations": [
                {"statement": "LOG-1 records the timeout and changed retry key.", "evidence_ids": ["LOG-1"]}
            ],
            "relationships": [],
            "hypotheses": [],
            "unresolved_questions": ["Did the provider settle both attempts?"],
        }
    )
    summarizer = BedrockContextSummarizer(
        model_id="test-model",
        client=client,
        prompt_default_arn="arn:aws:bedrock:us-east-1:123456789012:prompt/COMPACTION:1",
    )

    enriched = summarizer.summarize(context)

    assert enriched.semantic_summary["summary_evidence_ids"] == ["LOG-1"]
    assert enriched.digest != context.digest
    assert client.calls[0]["modelId"].endswith(":1")
    assert "context_schema_version" in client.calls[0]["promptVariables"]["input"]["text"]


def test_llm_semantic_pass_rejects_unsupported_citations():
    context = ContextCompactor(max_tokens=3000, reserved_tokens=800).compact(
        base_report=_report(),
        evidence=_evidence(2),
        memories=[],
        tenant_id="tenant_acme",
    )
    summarizer = BedrockContextSummarizer(
        model_id="test-model",
        client=_SummarizerClient(
            {
                "summary": "Unsupported claim.",
                "summary_evidence_ids": ["NOT-IN-CONTEXT"],
                "observations": [],
                "relationships": [],
                "hypotheses": [],
                "unresolved_questions": [],
            }
        ),
        prompt_default_arn="arn:aws:bedrock:us-east-1:123456789012:prompt/COMPACTION:1",
    )

    with pytest.raises(ContextSummarizationError, match="unsupported citation"):
        summarizer.summarize(context)
