"""Step Functions Lambda for the post-run deterministic investigation audit."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key

from ledgerflow.audit import audit_investigation


REGION = os.environ.get("AWS_REGION", "us-east-1")
CHECKPOINTS_TABLE_NAME = os.environ.get("WORKFLOW_CHECKPOINTS_TABLE", "")
EVENTS_TABLE_NAME = os.environ.get("WORKFLOW_EVENTS_TABLE", "")

dynamodb = boto3.resource("dynamodb", region_name=REGION)


def _state(checkpoint: dict[str, Any]) -> dict[str, Any]:
    raw = checkpoint.get("Data", "{}")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _evidence_packet(state: dict[str, Any]) -> list[dict[str, Any]]:
    packet: list[dict[str, Any]] = []
    for kind, items, identifier in (
        ("incident", [state.get("incident")] if isinstance(state.get("incident"), dict) else [], "IncidentId"),
        ("invoice", state.get("invoices", []), "InvoiceId"),
        ("application_log", state.get("logs", []), "LogId"),
        ("deployment", state.get("deployments", []), "DeploymentId"),
    ):
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                packet.append({"id": item.get(identifier), "kind": kind, "data": item})
    for item in state.get("vector_items", []) if isinstance(state.get("vector_items"), list) else []:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        packet.append({
            "id": metadata.get("document_id") or metadata.get("source_id") or str(item.get("key", "")).split("#", 1)[0],
            "kind": "vector_evidence",
            "data": item,
        })
    return packet


def _events(workflow_id: str) -> list[dict[str, Any]]:
    if not EVENTS_TABLE_NAME:
        return []
    response = dynamodb.Table(EVENTS_TABLE_NAME).query(
        KeyConditionExpression=Key("WorkflowId").eq(workflow_id),
        Limit=250,
        ScanIndexForward=True,
    )
    events: list[dict[str, Any]] = []
    for item in response.get("Items", []):
        payload = item.get("Payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        events.append({
            "event_type": item.get("EventType"),
            "step": item.get("Step"),
            "status": item.get("Status"),
            "payload": payload if isinstance(payload, dict) else {},
        })
    return events


def _write_audit_event(workflow_id: str, tenant_id: str, status: str, audit: dict[str, Any]) -> None:
    if not EVENTS_TABLE_NAME:
        return
    now = int(time.time())
    dynamodb.Table(EVENTS_TABLE_NAME).put_item(
        Item={
            "WorkflowId": workflow_id,
            "EventId": f"{time.time_ns()}#{uuid4().hex}",
            "TenantId": tenant_id,
            "EventType": "deterministic_audit",
            "Step": "post_run_audit",
            "Status": status,
            "Visibility": "operator",
            "Payload": json.dumps(audit, sort_keys=True, default=str)[:24000],
            "CreatedAt": now,
            "ExpiresAtEpoch": now + (30 * 24 * 60 * 60),
        }
    )


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    workflow_id = str(event.get("workflow_id") or "").strip()
    if not workflow_id:
        raise ValueError("workflow_id is required")
    checkpoint = dynamodb.Table(CHECKPOINTS_TABLE_NAME).get_item(
        Key={"WorkflowId": workflow_id},
        ConsistentRead=True,
    ).get("Item")
    if not checkpoint:
        raise ValueError(f"workflow checkpoint not found: {workflow_id}")

    checkpoint_tenant = str(checkpoint.get("TenantId") or "")
    tenant_id = str(event.get("tenant_id") or checkpoint_tenant)
    if checkpoint_tenant and tenant_id != checkpoint_tenant:
        raise PermissionError("workflow tenant does not match audit request")
    state = _state(checkpoint)
    report = state.get("report") if isinstance(state.get("report"), dict) else {}
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    customer_id = str(event.get("customer_id") or checkpoint.get("CustomerId") or report.get("customer_id") or "")
    audit = audit_investigation(
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
        plan=plan,
        report=report,
        evidence=_evidence_packet(state),
        memories=state.get("memories", []) if isinstance(state.get("memories"), list) else [],
        events=_events(workflow_id),
        required_evidence_ids=state.get("required_evidence_ids", []) if isinstance(state.get("required_evidence_ids"), list) else [],
        mode="post_run",
    )
    _write_audit_event(workflow_id, tenant_id, "completed" if audit["passed"] else "failed", audit)
    return audit
