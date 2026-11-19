"""Schema-driven evidence normalization and report construction.

The first demo report was tied to one fixture's renewal, deployment, and
payment identifiers.  This module keeps the safety boundary explicit while
deriving claims from the fields present in each evidence source.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .models import IncidentReport


EVIDENCE_SCHEMA_VERSION = "ledgerflow.evidence.v2"

# These aliases are the contract between AWS source adapters and the report
# builder. The reasoning code should not need to know which service supplied a
# record.
SOURCE_SCHEMA: dict[str, dict[str, tuple[str, ...]]] = {
    "incident": {
        "id": ("IncidentId", "incident_id", "evidence_id"),
        "customer": ("CustomerId", "customer_id"),
        "tenant": ("TenantId", "tenant_id"),
        "category": ("Category", "category"),
        "severity": ("Severity", "severity"),
        "description": ("Description", "description", "content"),
        "deployment": ("DeploymentVersion", "deployment_version"),
        "status": ("Status", "status"),
        "timestamp": ("CreatedAt", "created_at", "observed_at"),
    },
    "invoice": {
        "id": ("InvoiceId", "invoice_id", "evidence_id"),
        "customer": ("CustomerId", "customer_id"),
        "renewal": ("RenewalId", "renewal_id"),
        "subscription": ("SubscriptionId", "subscription_id"),
        "amount": ("AmountCents", "amount_cents"),
        "status": ("Status", "status"),
        "timestamp": ("IssuedAt", "issued_at", "observed_at"),
    },
    "application_log": {
        "id": ("LogId", "log_id", "evidence_id"),
        "event": ("Event", "event"),
        "service": ("Service", "service"),
        "timestamp": ("Timestamp", "timestamp", "observed_at"),
        "payload": ("PayloadJson", "payload_json", "payload"),
    },
    "deployment": {
        "id": ("DeploymentId", "deployment_id", "evidence_id"),
        "version": ("Version", "version"),
        "service": ("Service", "service"),
        "summary": ("ChangeSummary", "change_summary", "content"),
        "status": ("Status", "status"),
        "timestamp": ("DeployedAt", "deployed_at", "observed_at"),
    },
}


@dataclass
class EvidenceAnalysis:
    """Normalized observations and claims derived from collected evidence."""

    incident_id: str
    customer_id: str
    tenant_id: str
    category: str
    deployment_version: str | None
    evidence_ids: list[str] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    duplicate_groups: list[dict[str, Any]] = field(default_factory=list)
    retry_observations: list[dict[str, Any]] = field(default_factory=list)
    timeout_observations: list[dict[str, Any]] = field(default_factory=list)
    failure_observations: list[dict[str, Any]] = field(default_factory=list)
    relevant_deployments: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.45

    @property
    def claims(self) -> list[dict[str, Any]]:
        return [*self.facts, *self.relationships, *self.hypotheses]


def _record(item: Any) -> dict[str, Any]:
    """Normalize an AWS source record into the shared evidence mapping."""

    if isinstance(item, Mapping):
        value = dict(item)
        metadata = value.get("metadata")
        if isinstance(metadata, Mapping):
            for key, metadata_value in metadata.items():
                value.setdefault(str(key), metadata_value)
        return value
    return {}


def _value(item: Mapping[str, Any], source: str, field_name: str, default: Any = None) -> Any:
    for key in SOURCE_SCHEMA.get(source, {}).get(field_name, ()):
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _payload(item: Mapping[str, Any]) -> dict[str, Any]:
    value = _value(item, "application_log", "payload", None)
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            value = {}
        else:
            return dict(parsed) if isinstance(parsed, Mapping) else {}
    payload_keys = {
        "amount_cents",
        "attempt",
        "error",
        "idempotency_key",
        "invoice_id",
        "original_idempotency_key",
        "payment_id",
        "provider_status",
        "retry_idempotency_key",
        "renewal_id",
    }
    return {key: item[key] for key in payload_keys if key in item}


def _identifier(item: Mapping[str, Any], source: str) -> str | None:
    value = _value(item, source, "id")
    return str(value) if value not in (None, "") else None


def build_evidence_query(incident: Mapping[str, Any], logs: Iterable[Mapping[str, Any]] = ()) -> str:
    """Build a high-signal retrieval query from the source schema."""

    incident_record = _record(incident)
    terms: list[str] = []
    category = _value(incident_record, "incident", "category")
    description = _value(incident_record, "incident", "description")
    terms.extend([str(category or ""), str(description or "")])
    for item in logs:
        record = _record(item)
        event = _value(record, "application_log", "event")
        service = _value(record, "application_log", "service")
        terms.extend([str(event or ""), str(service or "")])
        payload = _payload(record)
        terms.extend(str(key).replace("_", " ") for key in payload)
        terms.extend(str(value) for key, value in payload.items() if key in {"error", "provider_status"})
        if _contains_timeout(event) or _contains_timeout(payload):
            terms.append("timeout")
    query = " ".join(term for term in terms if term).strip()
    return query or "incident evidence"


def _append_claim(target: list[dict[str, Any]], kind: str, statement: str, evidence_ids: Iterable[str], confidence: float | None = None) -> None:
    ids = list(dict.fromkeys(str(value) for value in evidence_ids if value))
    if not statement or (kind != "gap" and not ids):
        return
    claim: dict[str, Any] = {"type": kind, "statement": statement, "evidence_ids": ids}
    if confidence is not None:
        claim["confidence"] = round(max(0.0, min(1.0, confidence)), 2)
    target.append(claim)


def _unique(values: Iterable[str | None]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def _contains_timeout(value: Any) -> bool:
    return "timeout" in _text(value).lower() or "timed out" in _text(value).lower()


def _contains_failure(value: Any) -> bool:
    return bool(re.search(r"\b(?:fail(?:ed|ure)?|declin(?:ed|e)|rejected|invalid)\b", _text(value), flags=re.IGNORECASE))


def analyze_evidence(
    *,
    incident: Mapping[str, Any],
    invoices: Iterable[Mapping[str, Any]],
    logs: Iterable[Mapping[str, Any]],
    deployments: Iterable[Mapping[str, Any]],
    vectors: Iterable[Mapping[str, Any]],
    memories: Iterable[Mapping[str, Any]] = (),
    tenant_id: str = "",
) -> EvidenceAnalysis:
    """Derive typed evidence claims without relying on fixture identifiers."""

    incident_record = _record(incident)
    invoice_records = [_record(item) for item in invoices]
    log_records = [_record(item) for item in logs]
    deployment_records = [_record(item) for item in deployments]
    vector_records = [_record(item) for item in vectors]
    memory_records = [_record(item) for item in memories]

    incident_id = _identifier(incident_record, "incident") or "INC-UNKNOWN"
    customer_id = str(_value(incident_record, "incident", "customer", "UNKNOWN"))
    category = str(_value(incident_record, "incident", "category", "unknown"))
    incident_version = _value(incident_record, "incident", "deployment")
    incident_version = str(incident_version) if incident_version else None
    analysis = EvidenceAnalysis(
        incident_id=incident_id,
        customer_id=customer_id,
        tenant_id=tenant_id or str(_value(incident_record, "incident", "tenant", "")),
        category=category,
        deployment_version=incident_version,
    )

    all_records = [(incident_record, "incident"), *[(item, "invoice") for item in invoice_records], *[(item, "application_log") for item in log_records]]
    analysis.evidence_ids = _unique([_identifier(item, source) for item, source in all_records])
    for item in vector_records:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else item
        vector_id = metadata.get("document_id") or metadata.get("source_id") or item.get("key") or item.get("evidence_id")
        if vector_id:
            analysis.evidence_ids.append(str(vector_id).split("#", 1)[0])
    analysis.evidence_ids = _unique(analysis.evidence_ids)

    _append_claim(
        analysis.facts,
        "fact",
        f"{incident_id} is a {category.replace('_', ' ')} incident for {customer_id}.",
        [incident_id],
    )

    invoice_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in invoice_records:
        renewal = _value(item, "invoice", "renewal")
        subscription = _value(item, "invoice", "subscription")
        if renewal:
            invoice_groups[("renewal", str(renewal))].append(item)
        elif subscription:
            invoice_groups[("subscription", str(subscription))].append(item)
    for (group_type, group_value), items in invoice_groups.items():
        ids = _unique([_identifier(item, "invoice") for item in items])
        group = {"type": group_type, "value": group_value, "invoice_ids": ids, "count": len(ids)}
        analysis.duplicate_groups.append(group)
        if len(ids) > 1:
            _append_claim(
                analysis.facts,
                "fact",
                f"{len(ids)} invoices share {group_type} {group_value}.",
                ids,
            )
        elif ids:
            _append_claim(
                analysis.facts,
                "fact",
                f"One invoice is recorded for {group_type} {group_value}.",
                ids,
            )

    for item in log_records:
        log_id = _identifier(item, "application_log")
        payload = _payload(item)
        event = _text(_value(item, "application_log", "event"))
        log_text = f"{event} {_text(payload)} {_text(item.get('content', ''))}"
        is_timeout = _contains_timeout(log_text)
        is_failure = _contains_failure(log_text)
        if is_timeout:
            analysis.timeout_observations.append({"id": log_id, "payload": payload, "event": event})
        if is_failure:
            analysis.failure_observations.append({"id": log_id, "payload": payload, "event": event})
        original_key = payload.get("original_idempotency_key")
        retry_key = payload.get("retry_idempotency_key")
        if original_key and retry_key:
            observation = {
                "id": log_id,
                "original_key": str(original_key),
                "retry_key": str(retry_key),
                "changed": str(original_key) != str(retry_key),
                "timeout": is_timeout,
            }
            analysis.retry_observations.append(observation)
            if observation["changed"]:
                _append_claim(
                    analysis.relationships,
                    "relationship",
                    f"{log_id} records a retry key change from {original_key} to {retry_key}.",
                    [log_id],
                )
            else:
                _append_claim(
                    analysis.facts,
                    "fact",
                    f"{log_id} records a retry that reused idempotency key {original_key}.",
                    [log_id],
                )
        elif is_timeout:
            _append_claim(
                analysis.facts,
                "fact",
                f"{log_id or 'The application log'} records a payment timeout.",
                [log_id] if log_id else [],
            )

        provider_status = str(payload.get("provider_status", "")).lower()
        if provider_status in {"success", "successful", "succeeded", "settled"}:
            _append_claim(
                analysis.facts,
                "fact",
                f"{log_id} records a successful provider outcome after the observed event.",
                [log_id] if log_id else [],
            )

    matching_deployments: list[dict[str, Any]] = []
    for item in deployment_records:
        version = _value(item, "deployment", "version")
        if incident_version and str(version) != incident_version:
            continue
        matching_deployments.append(item)

    incident_timestamp = _timestamp(_value(incident_record, "incident", "timestamp"))
    prior_deployments = [
        item
        for item in matching_deployments
        if incident_timestamp is None
        or _timestamp(_value(item, "deployment", "timestamp")) is None
        or _timestamp(_value(item, "deployment", "timestamp")) < incident_timestamp
    ]
    candidate_deployments = prior_deployments or matching_deployments
    if candidate_deployments:
        latest_timestamp = max(
            (_timestamp(_value(item, "deployment", "timestamp")) or datetime.min.replace(tzinfo=timezone.utc))
            for item in candidate_deployments
        )
        analysis.relevant_deployments = [
            item
            for item in candidate_deployments
            if (_timestamp(_value(item, "deployment", "timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) == latest_timestamp
        ]

    for item in analysis.relevant_deployments:
        version = _value(item, "deployment", "version")
        deployment_id = _identifier(item, "deployment")
        summary = _text(_value(item, "deployment", "summary", "deployment change recorded")).rstrip(".")
        analysis.evidence_ids = _unique([*analysis.evidence_ids, deployment_id])
        _append_claim(
            analysis.facts,
            "fact",
            f"{deployment_id or 'A deployment'} version {version or 'unknown'} recorded this change: {summary}.",
            [deployment_id] if deployment_id else [],
        )

    vector_ids: list[str] = []
    runbook_ids: list[str] = []
    for item in vector_records:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else item
        vector_id = metadata.get("document_id") or metadata.get("source_id") or item.get("key") or item.get("evidence_id")
        vector_id = str(vector_id).split("#", 1)[0] if vector_id else None
        if vector_id:
            vector_ids.append(vector_id)
        source_type = _text(metadata.get("source_type", ""))
        title = _text(metadata.get("title", ""))
        if source_type == "runbook" or "runbook" in title.lower() or vector_id and vector_id.upper().startswith("RUNBOOK-"):
            if vector_id:
                runbook_ids.append(vector_id)
    if vector_ids:
        _append_claim(
            analysis.facts,
            "fact",
            f"Evidence retrieval returned {len(vector_ids)} indexed evidence records.",
            vector_ids,
        )
    if runbook_ids:
        _append_claim(
            analysis.relationships,
            "relationship",
            "Operational guidance was available to compare the observed behavior with the expected control.",
            runbook_ids,
        )

    duplicate_groups = [group for group in analysis.duplicate_groups if group["count"] > 1]
    mismatched_retries = [item for item in analysis.retry_observations if item["changed"]]
    reused_retries = [item for item in analysis.retry_observations if not item["changed"]]
    failure_text = " ".join(_text(item.get("payload")) for item in analysis.failure_observations).lower()
    has_configuration_failure = "config" in failure_text or "merchant" in failure_text

    if duplicate_groups and mismatched_retries:
        group = duplicate_groups[0]
        retry = mismatched_retries[0]
        deployment = analysis.relevant_deployments[0] if analysis.relevant_deployments else {}
        version = _value(deployment, "deployment", "version") or incident_version or "the active deployment"
        summary = _text(_value(deployment, "deployment", "summary", "the deployment changed retry behavior"))
        statement = (
            f"A retry with a regenerated idempotency key is the leading explanation for the duplicate "
            f"{group['type']} {group['value']}: {retry['original_key']} changed to {retry['retry_key']}, "
            f"and version {version} recorded this change ({summary})."
        )
        support_ids = [*group["invoice_ids"], retry["id"], *[_identifier(item, "deployment") for item in analysis.relevant_deployments], *runbook_ids]
        _append_claim(analysis.hypotheses, "hypothesis", statement, support_ids, confidence=0.9)
        analysis.unresolved_questions.append("Did the provider settle each payment attempt or only acknowledge the requests?")
    elif reused_retries and not any(group["count"] > 1 for group in analysis.duplicate_groups):
        retry = reused_retries[0]
        _append_claim(
            analysis.relationships,
            "relationship",
            f"The retry reused {retry['original_key']}; the collected invoices do not show a duplicate group.",
            [retry["id"]],
        )
        analysis.hypotheses.append(
            {
                "type": "hypothesis",
                "statement": f"Reusing idempotency key {retry['original_key']} is consistent with safe retry handling; the collected invoices do not show a duplicate operation.",
                "evidence_ids": [retry["id"], *[identifier for group in analysis.duplicate_groups for identifier in group["invoice_ids"]]],
                "confidence": 0.84,
            }
        )
    elif mismatched_retries:
        retry = mismatched_retries[0]
        _append_claim(
            analysis.hypotheses,
            "hypothesis",
            f"A timeout was followed by a changed idempotency key ({retry['original_key']} to {retry['retry_key']}), but the collected invoices do not prove a duplicate operation.",
            [retry["id"]],
            confidence=0.72,
        )
        analysis.unresolved_questions.append("What was the provider's final outcome for the retry with the changed idempotency key?")
    elif has_configuration_failure:
        failure_ids = [str(item["id"]) for item in analysis.failure_observations if item.get("id")]
        _append_claim(
            analysis.hypotheses,
            "hypothesis",
            "The payment failure is consistent with a merchant or payment configuration error in the application evidence.",
            failure_ids,
            confidence=0.78,
        )
        analysis.unresolved_questions.append("Was the configuration corrected and verified through a successful read-only payment attempt?")
    elif analysis.failure_observations:
        failure_ids = [str(item["id"]) for item in analysis.failure_observations if item.get("id")]
        _append_claim(
            analysis.hypotheses,
            "hypothesis",
            "The collected logs establish a payment failure, but do not establish a single underlying cause.",
            failure_ids,
            confidence=0.68,
        )
        analysis.unresolved_questions.append("What provider response or configuration state caused the payment failure?")
    elif analysis.timeout_observations:
        timeout_ids = [str(item["id"]) for item in analysis.timeout_observations if item.get("id")]
        _append_claim(
            analysis.hypotheses,
            "hypothesis",
            "The payment timeout requires provider reconciliation before it can be classified as success or failure.",
            timeout_ids,
            confidence=0.65,
        )
        analysis.unresolved_questions.append("What was the provider's final outcome for the timed-out payment?")
    else:
        analysis.unresolved_questions.append("Which additional source can establish the incident's causal mechanism?")

    if incident_version:
        analysis.unresolved_questions.append(f"Did deployment {incident_version} affect other customers in this tenant?")
    if not analysis.relevant_deployments and incident_version:
        analysis.unresolved_questions.append(f"Can deployment evidence for version {incident_version} be located?")

    source_types = {source for _, source in all_records if _identifier(_, source)}
    if analysis.relevant_deployments:
        source_types.add("deployment")
    support_strength = 0.45 + min(0.35, len(source_types) * 0.07)
    if duplicate_groups:
        support_strength += 0.08
    if mismatched_retries:
        support_strength += 0.1
    if analysis.relevant_deployments:
        support_strength += 0.05
    if runbook_ids:
        support_strength += 0.04
    if analysis.unresolved_questions:
        support_strength -= min(0.08, len(analysis.unresolved_questions) * 0.02)
    analysis.confidence = round(max(0.35, min(0.99, support_strength)), 2)
    return analysis


def _actions(analysis: EvidenceAnalysis) -> list[str]:
    actions: list[str] = []
    if analysis.duplicate_groups or analysis.retry_observations:
        actions.append("Reconcile provider payment status using the original payment identifier before any retry.")
    if any(item.get("changed") for item in analysis.retry_observations):
        actions.append("Review and roll forward the retry behavior through the approved change process.")
    if analysis.duplicate_groups:
        actions.append("Review duplicate invoices through the approved financial operations process.")
    if any("configuration" in _text(item.get("payload")).lower() or "merchant" in _text(item.get("payload")).lower() for item in analysis.failure_observations):
        actions.append("Correct the merchant configuration through the approved process and verify it with a read-only payment attempt.")
    if not actions:
        actions.append("Collect the missing provider or configuration evidence before taking corrective action.")
    return list(dict.fromkeys(actions))


def _timeline(analysis: EvidenceAnalysis) -> list[str]:
    timeline: list[str] = []
    for item in analysis.relevant_deployments:
        deployment_id = _identifier(item, "deployment")
        version = _value(item, "deployment", "version", "unknown")
        summary = _text(_value(item, "deployment", "summary", "deployment change recorded"))
        timeline.append(f"{deployment_id or 'Deployment'} version {version}: {summary}.")
    for item in analysis.retry_observations:
        if item["changed"]:
            timeline.append(f"{item['id']} recorded a timeout/retry with a changed idempotency key.")
        else:
            timeline.append(f"{item['id']} recorded a retry that reused the original idempotency key.")
    for group in analysis.duplicate_groups:
        if group["count"] > 1:
            timeline.append(f"{group['count']} invoices were created for {group['type']} {group['value']}.")
    if not timeline:
        for item in analysis.timeout_observations:
            timeline.append(f"{item.get('id') or 'Application logs'} recorded a payment timeout.")
        for item in analysis.failure_observations:
            timeline.append(f"{item.get('id') or 'Application logs'} recorded a payment failure.")
    return list(dict.fromkeys(timeline))[:8]


def build_evidence_report(
    *,
    incident: Mapping[str, Any],
    invoices: Iterable[Mapping[str, Any]],
    logs: Iterable[Mapping[str, Any]],
    deployments: Iterable[Mapping[str, Any]],
    vectors: Iterable[Mapping[str, Any]],
    memories: Iterable[Mapping[str, Any]] = (),
    tenant_id: str = "",
    task_type: str = "investigation",
    recovery_history: Iterable[str] = (),
) -> IncidentReport:
    """Build a report from schema-derived claims and source relationships."""

    memory_items = list(memories)
    analysis = analyze_evidence(
        incident=incident,
        invoices=invoices,
        logs=logs,
        deployments=deployments,
        vectors=vectors,
        memories=memory_items,
        tenant_id=tenant_id,
    )
    root_cause = analysis.hypotheses[0]["statement"] if analysis.hypotheses else "The available evidence is insufficient to establish a single root cause."
    category = analysis.category.replace("_", " ")
    version = analysis.deployment_version
    affected_scope = _unique([analysis.customer_id, analysis.tenant_id, category, version])
    verified = [claim["statement"] for claim in analysis.facts[:6]]
    if memory_items:
        verified.append(f"AgentCore Memory returned {len(memory_items)} eligible records.")
    else:
        verified.append("AgentCore Memory returned 0 eligible records.")
    summary = f"{analysis.incident_id} affected {analysis.customer_id}; the evidence describes a {category} investigation."
    if analysis.hypotheses:
        summary += " The leading evidence-supported explanation is included below."
    risks = ["This Runtime is read-only and cannot execute a financial correction."]
    if analysis.unresolved_questions:
        risks.append("Provider or downstream outcome remains unverified until the unresolved questions are checked.")
    return IncidentReport(
        incident_id=analysis.incident_id,
        customer_id=analysis.customer_id,
        root_cause=root_cause,
        evidence_ids=analysis.evidence_ids,
        confidence=analysis.confidence,
        recommended_actions=_actions(analysis),
        unresolved_questions=list(dict.fromkeys(analysis.unresolved_questions)),
        executive_summary=summary,
        affected_scope=affected_scope,
        incident_timeline=_timeline(analysis),
        verified_findings=verified,
        recovery_history=list(recovery_history),
        remaining_risks=risks,
        task_type=task_type,
        evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
        evidence_claims=analysis.claims,
    )
