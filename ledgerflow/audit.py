"""Deterministic investigation contract checks shared by Runtime and Lambda."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .retrieval_policies import policy_for


AUDIT_SCHEMA_VERSION = "ledgerflow.audit.v1"
EVIDENCE_SCHEMA_VERSION = "ledgerflow.evidence.v2"
CLAIM_TYPES = {"fact", "relationship", "hypothesis"}
READ_ONLY_TOOLS = {
    "search_incidents",
    "get_customer_invoices",
    "get_application_logs",
    "get_deployments",
    "get_current_invoice_state",
    "search_evidence",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in result:
            result.append(text)
    return result


def _record_id(item: Mapping[str, Any]) -> str:
    direct = item.get("id") or item.get("evidence_id")
    if direct:
        return _text(direct).split("#", 1)[0]
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    nested = metadata.get("document_id") or metadata.get("source_id") or item.get("key")
    if nested:
        return _text(nested).split("#", 1)[0]
    data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
    for key in ("IncidentId", "InvoiceId", "LogId", "DeploymentId", "id"):
        if data.get(key):
            return _text(data[key])
    return ""


def _tenant_values(item: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    mappings = [item]
    for key in ("data", "metadata"):
        nested = item.get(key)
        if isinstance(nested, Mapping):
            mappings.append(nested)
    for mapping in mappings:
        for key in ("TenantId", "tenant_id", "tenantId"):
            if mapping.get(key):
                values.append(_text(mapping[key]))
    return _unique(values)


def _check(checks: list[dict[str, Any]], name: str, passed: bool, **details: Any) -> None:
    checks.append({"name": name, "passed": bool(passed), "details": details})


def audit_investigation(
    *,
    workflow_id: str,
    tenant_id: str,
    customer_id: str,
    plan: Mapping[str, Any],
    report: Mapping[str, Any],
    evidence: Iterable[Mapping[str, Any]],
    memories: Iterable[Mapping[str, Any]] = (),
    events: Iterable[Mapping[str, Any]] = (),
    required_evidence_ids: Iterable[str] = (),
    mode: str = "pre_publish",
) -> dict[str, Any]:
    """Return a bounded, deterministic audit result for one investigation.

    The function intentionally checks structured contracts and identifiers, not
    the wording of an LLM-generated narrative. It is safe to run in the
    Runtime before publication and again in a post-run Step Functions audit.
    """

    evidence_items = [item for item in evidence if isinstance(item, Mapping)]
    memory_items = [item for item in memories if isinstance(item, Mapping)]
    event_items = [item for item in events if isinstance(item, Mapping)]
    checks: list[dict[str, Any]] = []

    task_type = _text(plan.get("task_type")) or "investigation"
    policy = policy_for(task_type)
    policy_id = _text(plan.get("retrieval_policy_id"))
    _check(
        checks,
        "retrieval_policy",
        task_type == policy.task_type and policy_id == policy.policy_id,
        task_type=task_type,
        expected_policy_id=policy.policy_id,
        actual_policy_id=policy_id,
    )

    report_incident = _text(report.get("incident_id"))
    report_customer = _text(report.get("customer_id"))
    _check(
        checks,
        "report_identity",
        report_incident != "" and report_customer == _text(customer_id),
        expected_customer_id=_text(customer_id),
        report_incident_id=report_incident,
        report_customer_id=report_customer,
    )
    _check(
        checks,
        "evidence_schema",
        _text(report.get("evidence_schema_version")) == EVIDENCE_SCHEMA_VERSION,
        expected=EVIDENCE_SCHEMA_VERSION,
        actual=_text(report.get("evidence_schema_version")),
    )
    _check(
        checks,
        "task_type_preserved",
        _text(report.get("task_type")) == policy.task_type,
        expected=policy.task_type,
        actual=_text(report.get("task_type")),
    )

    allowed_ids = _unique(_record_id(item) for item in evidence_items)
    report_ids = _unique(report.get("evidence_ids", [])) if isinstance(report.get("evidence_ids"), list) else []
    required_ids = _unique(required_evidence_ids)
    missing_required = sorted(set(required_ids) - set(report_ids))
    unauthorized_report_ids = sorted(set(report_ids) - set(allowed_ids))
    _check(
        checks,
        "evidence_set",
        not missing_required and not unauthorized_report_ids,
        required_evidence_ids=required_ids,
        missing_required_ids=missing_required,
        unauthorized_report_ids=unauthorized_report_ids,
        report_evidence_count=len(report_ids),
        allowed_evidence_count=len(allowed_ids),
    )

    invalid_claims: list[dict[str, Any]] = []
    claims = report.get("evidence_claims", [])
    if not isinstance(claims, list):
        claims = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            invalid_claims.append({"index": index, "reason": "claim is not an object"})
            continue
        claim_type = _text(claim.get("type"))
        claim_ids = _unique(claim.get("evidence_ids", [])) if isinstance(claim.get("evidence_ids"), list) else []
        invalid_ids = sorted(set(claim_ids) - set(allowed_ids))
        unreported_ids = sorted(set(claim_ids) - set(report_ids))
        if claim_type not in CLAIM_TYPES or not _text(claim.get("statement")) or invalid_ids or unreported_ids:
            invalid_claims.append(
                {
                    "index": index,
                    "type": claim_type,
                    "invalid_evidence_ids": invalid_ids,
                    "unreported_evidence_ids": unreported_ids,
                }
            )
    _check(
        checks,
        "claim_citations",
        not invalid_claims,
        claim_count=len(claims),
        invalid_claims=invalid_claims[:10],
    )

    source_counts: dict[str, int] = {}
    for item in evidence_items:
        kind = _text(item.get("kind")) or "unknown"
        source_counts[kind] = source_counts.get(kind, 0) + 1
    excluded_sources = {
        "invoice": not policy.include_invoices,
        "application_log": not policy.include_logs,
        "deployment": not policy.include_deployments,
    }
    excluded_observations = sorted(kind for kind, excluded in excluded_sources.items() if excluded and source_counts.get(kind, 0))
    _check(
        checks,
        "policy_source_scope",
        not excluded_observations,
        source_counts=source_counts,
        excluded_observations=excluded_observations,
    )
    _check(
        checks,
        "memory_boundary",
        policy.include_memory or not memory_items,
        memory_allowed=policy.include_memory,
        memory_count=len(memory_items),
    )

    tenant_violations: list[dict[str, Any]] = []
    for item in evidence_items:
        observed_tenants = [value for value in _tenant_values(item) if value != _text(tenant_id)]
        if observed_tenants:
            tenant_violations.append({"id": _record_id(item), "tenants": observed_tenants})
    _check(
        checks,
        "tenant_scope",
        not tenant_violations,
        tenant_id=_text(tenant_id),
        violations=tenant_violations[:10],
    )

    tool_violations: list[dict[str, Any]] = []
    for item in event_items:
        if _text(item.get("event_type")) != "tool_call":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        tool = _text(payload.get("tool"))
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), Mapping) else {}
        if tool and tool not in READ_ONLY_TOOLS:
            tool_violations.append({"tool": tool, "reason": "tool is outside the read-only allowlist"})
        observed_tenant = _text(arguments.get("tenant_id"))
        if observed_tenant and observed_tenant != _text(tenant_id):
            tool_violations.append({"tool": tool, "reason": "tool tenant mismatch"})
        if tool == "memory_lookup" and not policy.include_memory:
            tool_violations.append({"tool": tool, "reason": "memory excluded by policy"})
        if tool == "search_evidence":
            strategy = _text(arguments.get("strategy"))
            source_types = sorted(_text(value) for value in arguments.get("source_types", []) if _text(value))
            if strategy and strategy != policy.strategy:
                tool_violations.append({"tool": tool, "reason": "retrieval strategy mismatch", "actual": strategy, "expected": policy.strategy})
            if source_types != sorted(policy.vector_source_types):
                tool_violations.append({"tool": tool, "reason": "retrieval source filter mismatch", "actual": source_types, "expected": sorted(policy.vector_source_types)})
            try:
                if int(arguments.get("top_k", policy.top_k)) > policy.top_k:
                    tool_violations.append({"tool": tool, "reason": "top_k exceeds policy limit"})
            except (TypeError, ValueError):
                tool_violations.append({"tool": tool, "reason": "top_k is not numeric"})
        if tool == "get_application_logs":
            try:
                if int(arguments.get("limit", policy.log_limit)) > policy.log_limit:
                    tool_violations.append({"tool": tool, "reason": "log limit exceeds policy limit"})
            except (TypeError, ValueError):
                tool_violations.append({"tool": tool, "reason": "log limit is not numeric"})
    _check(
        checks,
        "read_only_tool_contract",
        not tool_violations,
        violations=tool_violations[:10],
    )

    failed_checks = [check["name"] for check in checks if not check["passed"]]
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "mode": _text(mode) or "pre_publish",
        "workflow_id": _text(workflow_id),
        "task_type": policy.task_type,
        "policy_id": policy.policy_id,
        "passed": not failed_checks,
        "check_count": len(checks),
        "passed_count": len(checks) - len(failed_checks),
        "failed_count": len(failed_checks),
        "failed_checks": failed_checks,
        "checks": checks,
        "report_evidence_ids": report_ids,
        "allowed_evidence_ids": allowed_ids,
    }
