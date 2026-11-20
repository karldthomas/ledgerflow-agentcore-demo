from __future__ import annotations

from ledgerflow.audit import audit_investigation


def _case() -> dict[str, object]:
    evidence = [
        {"id": "INC-1042", "kind": "incident", "data": {"IncidentId": "INC-1042", "CustomerId": "ACME-482", "TenantId": "tenant_acme"}},
        {"id": "INV-8821", "kind": "invoice", "data": {"InvoiceId": "INV-8821", "CustomerId": "ACME-482", "TenantId": "tenant_acme"}},
        {"id": "RUNBOOK-014", "kind": "vector_evidence", "data": {"metadata": {"source_id": "RUNBOOK-014", "tenant_id": "public"}}},
    ]
    return {
        "workflow_id": "audit-1",
        "tenant_id": "tenant_acme",
        "customer_id": "ACME-482",
        "plan": {
            "task_type": "verification",
            "retrieval_policy_id": "verification-v1",
        },
        "report": {
            "incident_id": "INC-1042",
            "customer_id": "ACME-482",
            "task_type": "verification",
            "evidence_schema_version": "ledgerflow.evidence.v2",
            "evidence_ids": ["INC-1042", "INV-8821", "RUNBOOK-014"],
            "evidence_claims": [
                {"type": "fact", "statement": "The incident is known.", "evidence_ids": ["INC-1042"]},
            ],
        },
        "evidence": evidence,
    }


def test_audit_passes_for_a_grounded_read_only_report():
    case = _case()
    result = audit_investigation(**case, required_evidence_ids=["INC-1042", "INV-8821"])

    assert result["passed"] is True
    assert result["failed_checks"] == []
    assert result["schema_version"] == "ledgerflow.audit.v1"


def test_audit_rejects_unauthorized_citations_and_policy_memory_use():
    case = _case()
    case["report"]["evidence_ids"] = ["INC-1042", "NOT-IN-EVIDENCE"]
    case["report"]["evidence_claims"] = [
        {"type": "hypothesis", "statement": "Unsupported.", "evidence_ids": ["NOT-IN-EVIDENCE"]},
    ]
    result = audit_investigation(**case, memories=[{"id": "memory-1"}])

    assert result["passed"] is False
    assert "evidence_set" in result["failed_checks"]
    assert "claim_citations" in result["failed_checks"]
    assert "memory_boundary" in result["failed_checks"]
