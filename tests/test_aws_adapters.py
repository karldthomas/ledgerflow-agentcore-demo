from __future__ import annotations

import json
import io
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

from ledgerflow.gateway import AgentCoreGatewayClient
from ledgerflow.cloud_agent import CloudInvestigationHandler, DeterministicFailure
from ledgerflow.models import IncidentReport
from ledgerflow.prompting import PromptVersionResolver
from ledgerflow.reasoning import BedrockReportReasoner
from ledgerflow.runtime_entry import RuntimeHandler


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _CheckpointTable:
    def __init__(self):
        self.item: dict[str, object] | None = None

    def get_item(self, **_kwargs):
        return {"Item": dict(self.item)} if self.item else {}

    def update_item(self, *, ExpressionAttributeValues, UpdateExpression, ConditionExpression=None, **_kwargs):
        values = ExpressionAttributeValues
        if UpdateExpression.startswith("REMOVE"):
            if self.item and self.item.get("LeaseOwner") != values[":owner"]:
                raise _ConditionalFailure()
            if self.item:
                self.item.pop("LeaseOwner", None)
                self.item.pop("LeaseExpiresAt", None)
            return {}

        if ConditionExpression and "LeaseOwner" in ConditionExpression:
            now = int(values.get(":now", values.get(":lease_now", 0)))
            lease_owner = self.item.get("LeaseOwner") if self.item else None
            lease_expires = int(self.item.get("LeaseExpiresAt", 0)) if self.item else 0
            allowed = not lease_owner or lease_expires < now or lease_owner == values[":owner"]
            if not allowed:
                raise _ConditionalFailure()

        if self.item is None:
            self.item = {}
        if ":expires" in values and ":customer" not in values:
            self.item["LeaseExpiresAt"] = values[":expires"]
            self.item["UpdatedAt"] = values[":updated"]
            return {}
        if ":zero" in values:
            self.item["CheckpointVersion"] = int(self.item.get("CheckpointVersion", 0)) + 1
            self.item.update(
                {
                    "CustomerId": values[":customer"],
                    "TenantId": values[":tenant"],
                    "CurrentStep": values[":step"],
                    "Data": values[":data"],
                    "CompletedSteps": values[":completed"],
                    "StateSchemaVersion": values[":schema"],
                    "Status": values[":status"],
                    "FailureInjectionConsumed": values[":failure_consumed"],
                    "UpdatedAt": values[":updated"],
                    "LeaseOwner": values[":owner"],
                    "LeaseExpiresAt": values[":lease_expires"],
                }
            )
        else:
            self.item.update(
                {
                    "CustomerId": values[":customer"],
                    "TenantId": values[":tenant"],
                    "StateSchemaVersion": values[":schema"],
                    "Status": values[":status"],
                    "LeaseOwner": values[":owner"],
                    "LeaseExpiresAt": values[":expires"],
                }
            )
        return {}


class _DynamoResource:
    def __init__(self, checkpoint_table):
        self.checkpoint_table = checkpoint_table

    def Table(self, _name):
        return self.checkpoint_table


class _OperationLedgerTable:
    def __init__(self, item=None):
        self.item = dict(item) if item else None
        self.last_put: dict[str, object] | None = None

    def put_item(self, *, Item, ConditionExpression=None, **kwargs):
        if self.item is not None and ConditionExpression == "attribute_not_exists(OperationId)":
            raise _ConditionalFailure()
        if self.item is not None and ConditionExpression:
            values = kwargs["ExpressionAttributeValues"]
            current_owner = self.item.get("Owner")
            current_expiry = int(self.item.get("ExpiresAtEpoch", 0) or 0)
            owner = values[":owner"]
            now = int(values[":now"])
            allowed = (
                self.item.get("Status") == values[":started"]
                and (not current_owner or current_owner == owner or current_expiry < now)
            )
            if not allowed:
                raise _ConditionalFailure()
        self.last_put = {"Item": dict(Item), "ConditionExpression": ConditionExpression, **kwargs}
        self.item = dict(Item)
        return {}

    def get_item(self, **_kwargs):
        return {"Item": dict(self.item)} if self.item else {}


class _OperationLedgerResource:
    def __init__(self, operation_table):
        self.operation_table = operation_table

    def Table(self, _name):
        return self.operation_table


class _DevToolsTable:
    def __init__(self, item):
        self.item = item

    def query(self, **_kwargs):
        return {"Items": [dict(self.item)]}

    def update_item(self, *, ExpressionAttributeValues, **_kwargs):
        if self.item.get("Consumed") or not self.item.get("Enabled"):
            raise _ConditionalFailure()
        self.item.update(
            {
                "Consumed": ExpressionAttributeValues[":true"],
                "ConsumedAt": ExpressionAttributeValues[":updated"],
                "ConsumedBy": ExpressionAttributeValues[":worker"],
                "UpdatedAt": ExpressionAttributeValues[":updated"],
            }
        )
        return {}


class _DynamoResourceWithDevTools(_DynamoResource):
    def __init__(self, checkpoint_table, dev_tools_table):
        super().__init__(checkpoint_table)
        self.dev_tools_table = dev_tools_table

    def Table(self, name):
        return self.dev_tools_table if name == "dev-tools" else self.checkpoint_table


class _InvestigationGateway:
    def __init__(self):
        self.calls: list[str] = []

    def call_tool(self, name, _arguments):
        tool = name.split("___", 1)[-1]
        self.calls.append(tool)
        responses = {
            "search_incidents": {
                "items": [
                    {
                        "IncidentId": "INC-1042",
                        "CustomerId": "ACME-482",
                        "TenantId": "tenant_acme",
                        "Category": "duplicate_invoice",
                        "Description": "Customer received two invoices for one renewal.",
                        "DeploymentVersion": "3.8.2",
                        "CreatedAt": "2026-11-15T10:45:00+00:00",
                    }
                ]
            },
            "get_customer_invoices": {
                "items": [
                    {"InvoiceId": "INV-8821", "CustomerId": "ACME-482", "RenewalId": "REN-2026-11"},
                    {"InvoiceId": "INV-8822", "CustomerId": "ACME-482", "RenewalId": "REN-2026-11"},
                ]
            },
            "get_application_logs": {
                "items": [
                    {
                        "LogId": "LOG-8821",
                        "CustomerId": "ACME-482",
                        "Timestamp": "2026-11-15T10:42:04+00:00",
                        "Service": "payment-retry-handler",
                        "Event": "payment_timeout",
                        "PayloadJson": {
                            "original_idempotency_key": "pay_1",
                            "retry_idempotency_key": "pay_2",
                        },
                    }
                ]
            },
            "get_deployments": {
                "items": [
                    {
                        "DeploymentId": "DEP-382",
                        "Version": "3.8.2",
                        "DeployedAt": "2026-11-14T18:00:00+00:00",
                        "Service": "payment-retry-handler",
                        "ChangeSummary": "Retry key is regenerated after a timeout.",
                    }
                ]
            },
            "search_evidence": {
                "items": [
                    {
                        "key": "RUNBOOK-014#0",
                        "metadata": {
                            "document_id": "RUNBOOK-014",
                            "source_id": "RUNBOOK-014",
                            "source_type": "runbook",
                            "title": "Idempotent Retry Handling",
                            "text": "Retries reuse the original idempotency key.",
                        },
                    }
                ]
            },
        }
        return responses[tool]


def test_gateway_initializes_mcp_session_and_reuses_session_header(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        calls.append({"body": body, "headers": dict(request.headers), "timeout": timeout})
        if body["method"] == "initialize":
            return _Response(
                json.dumps({"result": {"protocolVersion": "2025-11-25", "capabilities": {}}}).encode(),
                {"Mcp-Session-Id": "session-1"},
            )
        if body["method"] == "notifications/initialized":
            return _Response(b"", {"Mcp-Session-Id": "session-1"})
        return _Response(
            json.dumps({"result": {"content": [{"type": "text", "text": '{"items": [{"id": "INC-1"}]}'}]}}).encode(),
            {"Mcp-Session-Id": "session-1"},
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = AgentCoreGatewayClient("https://gateway.example", protocol_version="2025-11-25")
    client._signed_headers = lambda _body, session_id=None: {"Mcp-Session-Id": session_id} if session_id else {}

    assert client.call_tool("Incidents___search_incidents", {"tenant_id": "tenant_acme"}) == {"items": [{"id": "INC-1"}]}
    assert [call["body"]["method"] for call in calls] == ["initialize", "notifications/initialized", "tools/call"]
    assert "Mcp-Session-Id" not in calls[0]["headers"]
    assert calls[1]["headers"]["Mcp-session-id"] == "session-1"
    assert calls[2]["headers"]["Mcp-session-id"] == "session-1"


def test_gateway_reinitializes_expired_session_and_selects_matching_sse_frame(monkeypatch):
    calls: list[dict[str, object]] = []
    expired = False

    def fake_urlopen(request, timeout):
        nonlocal expired
        body = json.loads(request.data.decode("utf-8"))
        calls.append({"body": body, "headers": dict(request.headers), "timeout": timeout})
        if body["method"] == "initialize":
            session = "session-1" if not expired else "session-2"
            return _Response(
                json.dumps({"result": {"protocolVersion": "2025-11-25", "capabilities": {}}}).encode(),
                {"Mcp-Session-Id": session},
            )
        if body["method"] == "notifications/initialized":
            return _Response(b"", {"Mcp-Session-Id": "session-1" if not expired else "session-2"})
        if not expired:
            expired = True
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "session expired",
                hdrs={},
                fp=io.BytesIO(b"session expired"),
            )
        matching = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"content": [{"type": "text", "text": '{"items": [{"id": "INC-1"}]}' }]},
        }
        unrelated = {"jsonrpc": "2.0", "id": "unrelated", "result": {"content": []}}
        payload = f"data: {json.dumps(unrelated)}\n\ndata: {json.dumps(matching)}\n\n".encode()
        return _Response(payload, {"Mcp-Session-Id": "session-2"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = AgentCoreGatewayClient("https://gateway.example", protocol_version="2025-11-25")
    client._signed_headers = lambda _body, session_id=None: {"Mcp-Session-Id": session_id} if session_id else {}

    assert client.call_tool("Incidents___search_incidents", {"tenant_id": "tenant_acme"}) == {"items": [{"id": "INC-1"}]}
    assert [call["body"]["method"] for call in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]


class _ConverseClient:
    def __init__(self):
        self.calls = []

    def converse(self, **_kwargs):
        self.calls.append(_kwargs)
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "root_cause": "The retry key changed after a timeout.",
                                    "confidence": 0.91,
                                    "evidence_ids": ["LOG-1", "UNAUTHORIZED-ID"],
                                    "executive_summary": "Duplicate invoices followed a regenerated retry key.",
                                    "verified_findings": ["The retry log records a changed key."],
                                    "incident_timeline": ["The timeout preceded the retry."],
                                    "unresolved_questions": ["Did the provider settle both attempts?"],
                                    "recommended_actions": ["Refund the customer", "Reconcile provider status"],
                                    "remaining_risks": ["The agent remains read-only."],
                                }
                            )
                        }
                    ]
                }
            }
        }


def test_bedrock_reasoner_keeps_grounded_ids_and_blocks_financial_actions():
    base = IncidentReport(
        incident_id="INC-1",
        customer_id="ACME-482",
        root_cause="The deterministic evidence indicates a regenerated retry key.",
        evidence_ids=["INC-1", "LOG-1"],
        confidence=0.8,
        recommended_actions=["Review the incident through the approved process."],
        unresolved_questions=["Unknown"],
    )
    client = _ConverseClient()
    reasoner = BedrockReportReasoner(
        model_id="test-model",
        client=client,
        prompt_default_arn="arn:aws:bedrock:us-east-1:123456789012:prompt/SYNTHESIS:1",
    )
    report = reasoner.synthesize(
        base,
        evidence=[{"id": "INC-1", "kind": "incident", "data": {}}, {"id": "LOG-1", "kind": "log", "data": {}}],
        memories=[],
    )

    assert report.root_cause == "The deterministic evidence indicates a regenerated retry key."
    assert report.evidence_ids == ["INC-1", "LOG-1"]
    assert report.recommended_actions == ["Review the incident through the approved process."]
    call = client.calls[0]
    assert call["modelId"] == "arn:aws:bedrock:us-east-1:123456789012:prompt/SYNTHESIS:1"
    assert "system" not in call
    assert "messages" not in call
    assert "inferenceConfig" not in call
    assert call["promptVariables"]["input"]["text"]


def test_prompt_version_resolver_prefers_persisted_active_version():
    class _Table:
        def get_item(self, **_kwargs):
            return {"Item": {"ActiveArn": "arn:aws:bedrock:us-east-1:123456789012:prompt/PLANNER:7"}}

    resolver = PromptVersionResolver(
        prompt_arn="arn:aws:bedrock:us-east-1:123456789012:prompt/PLANNER",
        default_version_arn="arn:aws:bedrock:us-east-1:123456789012:prompt/PLANNER:1",
        selection_table=_Table(),
        prompt_key="planning",
    )
    assert resolver.resolve().endswith(":7")


def test_public_reasoning_events_redact_model_prompts():
    handler = object.__new__(CloudInvestigationHandler)
    events: list[tuple[object, ...]] = []
    handler._emit = lambda *args, **kwargs: events.append((args, kwargs))

    handler._planner_event(
        {
            "event": "llm_plan",
            "model_id": "test-model",
            "prompt_preview": "private prompt text",
            "plan": {"evidence_query": "duplicate invoice timeout"},
        }
    )
    handler._reasoning_event(
        {
            "event": "llm_synthesis_started",
            "model_id": "test-model",
            "prompt_preview": "private synthesis prompt",
        }
    )

    public_payloads = [call[0][3] for call in events if call[1].get("visibility") == "operator"]
    assert public_payloads[0]["prompt_redacted"] is True
    assert "prompt_preview" not in public_payloads[0]
    assert public_payloads[1]["prompt_redacted"] is True
    assert "prompt_preview" not in public_payloads[1]


def test_report_publish_recovers_expired_started_operation(monkeypatch):
    monkeypatch.setenv("OPERATION_LEDGER_TABLE", "operation-ledger")
    operation_table = _OperationLedgerTable(
        {
            "OperationId": "report:wf-recovery",
            "WorkflowId": "wf-recovery",
            "OperationType": "publish_report",
            "Status": "STARTED",
            "Owner": "dead-worker",
            "StartedAt": "2026-11-20T14:00:00+00:00",
            "ExpiresAtEpoch": 1,
        }
    )
    handler = object.__new__(CloudInvestigationHandler)
    handler.dynamodb = _OperationLedgerResource(operation_table)
    handler.worker_id = "recovery-worker"
    handler.lease_seconds = 5
    handler._renew_workflow_lease = lambda _workflow_id: None
    report = IncidentReport(
        incident_id="INC-1",
        customer_id="ACME-482",
        root_cause="A retry key changed after a timeout.",
        evidence_ids=["INC-1"],
        confidence=0.9,
        recommended_actions=["Reconcile the original payment."],
        unresolved_questions=["Was the original payment settled?"],
    )

    assert handler._publish_idempotently("wf-recovery", report) == report
    assert operation_table.item["Status"] == "SUCCEEDED"
    assert operation_table.last_put is not None
    assert operation_table.last_put["ExpressionAttributeNames"]["#owner"] == "Owner"
    assert operation_table.last_put["ConditionExpression"].startswith("#status = :started")


def test_result_previews_are_bounded_and_allowlisted():
    preview = CloudInvestigationHandler._result_preview(
        "get_application_logs",
        [
            {
                "LogId": "LOG-1",
                "CustomerId": "ACME-482",
                "TenantId": "tenant_acme",
                "Timestamp": "2026-11-15T10:42:00Z",
                "Service": "payment-retry-handler",
                "Event": "retry_attempt",
                "PayloadJson": {
                    "original_idempotency_key": "pay_1",
                    "retry_idempotency_key": "pay_2",
                    "private_field": "must not be shown",
                },
            }
        ],
    )

    assert preview == [
        {
            "LogId": "LOG-1",
            "CustomerId": "ACME-482",
            "Timestamp": "2026-11-15T10:42:00Z",
            "Service": "payment-retry-handler",
            "Event": "retry_attempt",
            "PayloadJson": {"original_idempotency_key": "pay_1", "retry_idempotency_key": "pay_2"},
        }
    ]


def test_runtime_entry_delegates_to_the_aws_handler_only(monkeypatch):
    calls: list[dict[str, object]] = []

    class _Handler:
        def __init__(self):
            calls.append({"event": "init"})

        def handle(self, payload):
            calls.append({"event": "handle", "payload": payload})
            return {"status": "ok"}

    monkeypatch.setattr("ledgerflow.runtime_entry.CloudInvestigationHandler", _Handler)
    runtime = RuntimeHandler()

    assert runtime.handle_invocation({"customer_id": "ACME-482"}) == {"status": "ok"}
    runtime.close()
    assert calls == [
        {"event": "init"},
        {"event": "handle", "payload": {"customer_id": "ACME-482"}},
    ]


def test_runtime_invocation_context_isolated_for_concurrent_requests():
    from ledgerflow import cloud_agent as cloud_agent_module

    handler = object.__new__(CloudInvestigationHandler)
    handler.worker_id = "test-worker"
    barrier = threading.Barrier(2)
    observed: list[tuple[str, str, str, str]] = []

    def fake_handle(payload):
        context = cloud_agent_module._INVOCATION_CONTEXT.get()
        assert context is not None
        barrier.wait(timeout=2)
        # Keep both invocations overlapped long enough to expose shared fields.
        observed.append((payload["workflow_id"], context.workflow_id, context.tenant_id, context.owner))
        return {"workflow_id": context.workflow_id, "tenant_id": context.tenant_id}

    handler._handle = fake_handle
    payloads = [
        {"workflow_id": "wf-a", "customer_id": "customer-a", "tenant_id": "tenant-a"},
        {"workflow_id": "wf-b", "customer_id": "customer-b", "tenant_id": "tenant-b"},
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(handler.handle, payloads))

    assert sorted(results, key=lambda item: item["workflow_id"]) == [
        {"workflow_id": "wf-a", "tenant_id": "tenant-a"},
        {"workflow_id": "wf-b", "tenant_id": "tenant-b"},
    ]
    assert {(row[0], row[1], row[2]) for row in observed} == {
        ("wf-a", "wf-a", "tenant-a"),
        ("wf-b", "wf-b", "tenant-b"),
    }
    assert len({row[3] for row in observed}) == 2


def test_large_checkpoint_state_is_spilled_and_integrity_checked(monkeypatch):
    class _Body:
        def __init__(self, value):
            self.value = value

        def read(self):
            return self.value

    class _S3:
        def __init__(self):
            self.objects = {}

        def put_object(self, *, Bucket, Key, Body, **_kwargs):
            self.objects[(Bucket, Key)] = Body

        def get_object(self, *, Bucket, Key):
            return {"Body": _Body(self.objects[(Bucket, Key)])}

    monkeypatch.setenv("RAW_DATA_BUCKET", "raw-bucket")
    monkeypatch.setenv("WORKFLOW_CHECKPOINT_MAX_BYTES", "64000")
    handler = object.__new__(CloudInvestigationHandler)
    handler.s3 = _S3()
    state = {"logs": ["x" * 70_000]}

    pointer = json.loads(handler._checkpoint_state("wf-large", "log_lookup", state))
    restored = handler._restore_checkpoint_state(pointer)

    assert pointer["__state_blob__"]["bucket"] == "raw-bucket"
    assert restored == state


def test_runtime_resumes_after_one_shot_checkpoint_failure(monkeypatch):
    monkeypatch.setenv("WORKFLOW_CHECKPOINTS_TABLE", "checkpoints")
    monkeypatch.setenv("LEDGERFLOW_ENABLE_FAILURE_INJECTION", "ENABLED")
    monkeypatch.delenv("WORKFLOW_EVENTS_TABLE", raising=False)
    monkeypatch.delenv("OPERATION_LEDGER_TABLE", raising=False)
    monkeypatch.delenv("REPORTS_BUCKET", raising=False)
    monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)

    checkpoint_table = _CheckpointTable()
    gateway = _InvestigationGateway()
    handler = object.__new__(CloudInvestigationHandler)
    handler.dynamodb = _DynamoResource(checkpoint_table)
    handler.gateway = gateway
    handler.reasoner = None
    handler.planner = None
    handler.memory = None
    handler.s3 = None
    handler.worker_id = "test-worker"
    handler.lease_seconds = 5

    payload = {
        "workflow_id": "recovery-demo-1",
        "customer_id": "ACME-482",
        "tenant_id": "tenant_acme",
        "request": "Investigate the duplicate invoice.",
        "failure_after_step": "log_lookup",
    }

    with pytest.raises(DeterministicFailure, match="log_lookup"):
        handler.handle(payload)

    assert checkpoint_table.item["CurrentStep"] == "log_lookup"
    assert "log_lookup" in checkpoint_table.item["CompletedSteps"]
    assert checkpoint_table.item["FailureInjectionConsumed"] is True
    assert gateway.calls == ["search_incidents", "get_customer_invoices", "get_application_logs"]

    resumed_handler = object.__new__(CloudInvestigationHandler)
    resumed_handler.dynamodb = _DynamoResource(checkpoint_table)
    resumed_handler.gateway = gateway
    resumed_handler.reasoner = None
    resumed_handler.planner = None
    resumed_handler.memory = None
    resumed_handler.s3 = None
    resumed_handler.worker_id = "restarted-worker"
    resumed_handler.lease_seconds = 5

    report = resumed_handler.handle(payload)

    assert report["incident_id"] == "INC-1042"
    assert checkpoint_table.item["CurrentStep"] == "completed"
    assert checkpoint_table.item["Status"] == "COMPLETED"
    checkpoint_state = json.loads(checkpoint_table.item["Data"])
    assert checkpoint_state["context_manifest"]["schema_version"] == "ledgerflow.context.v1"
    assert "INC-1042" in checkpoint_state["context_manifest"]["selected_evidence_ids"]
    assert gateway.calls == [
        "search_incidents",
        "get_customer_invoices",
        "get_application_logs",
        "get_deployments",
        "search_evidence",
    ]

    # A completed workflow is hydrated from its checkpoint instead of
    # invoking any evidence tools again.
    assert resumed_handler.handle(payload) == report
    assert gateway.calls == [
        "search_incidents",
        "get_customer_invoices",
        "get_application_logs",
        "get_deployments",
        "search_evidence",
    ]


def test_runtime_claims_admin_dev_tools_failure_plan_once(monkeypatch):
    monkeypatch.setenv("WORKFLOW_CHECKPOINTS_TABLE", "checkpoints")
    monkeypatch.setenv("DEV_TOOLS_CONTROLS_TABLE", "dev-tools")
    monkeypatch.setenv("LEDGERFLOW_ENVIRONMENT", "dev")
    monkeypatch.delenv("LEDGERFLOW_ENABLE_FAILURE_INJECTION", raising=False)
    monkeypatch.delenv("WORKFLOW_EVENTS_TABLE", raising=False)
    monkeypatch.delenv("OPERATION_LEDGER_TABLE", raising=False)
    monkeypatch.delenv("REPORTS_BUCKET", raising=False)
    monkeypatch.delenv("AGENTCORE_MEMORY_ID", raising=False)

    checkpoint_table = _CheckpointTable()
    dev_tools_table = _DevToolsTable(
        {
            "ControlId": "failure-control-1",
            "Environment": "dev",
            "WorkflowId": "dev-tools-recovery-1",
            "Step": "log_lookup",
            "Enabled": True,
            "Consumed": False,
            "CreatedAt": "2026-11-19T00:00:00Z",
            "ExpiresAtEpoch": 4_102_444_800,
        }
    )
    gateway = _InvestigationGateway()
    handler = object.__new__(CloudInvestigationHandler)
    handler.dynamodb = _DynamoResourceWithDevTools(checkpoint_table, dev_tools_table)
    handler.gateway = gateway
    handler.reasoner = None
    handler.planner = None
    handler.memory = None
    handler.s3 = None
    handler.worker_id = "test-worker"
    handler.lease_seconds = 5

    payload = {
        "workflow_id": "dev-tools-recovery-1",
        "customer_id": "ACME-482",
        "tenant_id": "tenant_acme",
        "request": "Investigate the duplicate invoice.",
    }

    with pytest.raises(DeterministicFailure, match="log_lookup"):
        handler.handle(payload)

    assert dev_tools_table.item["Consumed"] is True
    assert checkpoint_table.item["CurrentStep"] == "log_lookup"

    resumed_handler = object.__new__(CloudInvestigationHandler)
    resumed_handler.dynamodb = _DynamoResourceWithDevTools(checkpoint_table, dev_tools_table)
    resumed_handler.gateway = gateway
    resumed_handler.reasoner = None
    resumed_handler.planner = None
    resumed_handler.memory = None
    resumed_handler.s3 = None
    resumed_handler.worker_id = "restarted-worker"
    resumed_handler.lease_seconds = 5

    report = resumed_handler.handle(payload)

    assert report["incident_id"] == "INC-1042"
    assert checkpoint_table.item["Status"] == "COMPLETED"
    assert gateway.calls == [
        "search_incidents",
        "get_customer_invoices",
        "get_application_logs",
        "get_deployments",
        "search_evidence",
    ]
