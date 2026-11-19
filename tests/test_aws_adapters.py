from __future__ import annotations

import json

from ledgerflow.gateway import AgentCoreGatewayClient
from ledgerflow.cloud_agent import CloudInvestigationHandler
from ledgerflow.models import IncidentReport
from ledgerflow.prompting import PromptVersionResolver
from ledgerflow.reasoning import BedrockReportReasoner


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

    assert report.root_cause == "The retry key changed after a timeout."
    assert report.evidence_ids == ["INC-1", "LOG-1"]
    assert report.recommended_actions == ["Reconcile provider status"]
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
