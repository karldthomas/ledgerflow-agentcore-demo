"""AWS-backed investigation handler used by the Runtime container.

This adapter keeps the local deterministic workflow's report contract while
moving business-data access behind AgentCore Gateway, checkpoints into
DynamoDB, and long-term memory into AgentCore Memory.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .evidence_reasoning import build_evidence_report
from .gateway import AgentCoreGatewayClient
from .models import IncidentReport
from .planner import BedrockInvestigationPlanner, InvestigationPlan
from .reasoning import BedrockReportReasoner, ModelReasoningError


class CloudInvestigationHandler:
    def __init__(self, *, region: str | None = None) -> None:
        self.region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        gateway_url = os.environ["AGENTCORE_GATEWAY_URL"]
        self.gateway = AgentCoreGatewayClient(gateway_url, region=self.region)
        try:
            import boto3

            self.dynamodb = boto3.resource("dynamodb", region_name=self.region)
            self.s3 = boto3.client("s3", region_name=self.region)
            self.memory = boto3.client("bedrock-agentcore", region_name=self.region)
        except ImportError as exc:
            raise RuntimeError("boto3 is required for the AWS Runtime backend") from exc
        self.current_workflow_id: str | None = None
        self.reasoner: BedrockReportReasoner | None = None
        self.planner: BedrockInvestigationPlanner | None = None
        if os.environ.get("MODEL_REASONING_ENABLED", "DISABLED").upper() == "ENABLED":
            model_id = os.environ.get("MODEL_ID", "").strip()
            if model_id:
                selection_table_name = os.environ.get("PROMPT_SELECTIONS_TABLE", "").strip()
                selection_table = self.dynamodb.Table(selection_table_name) if selection_table_name else None
                self.reasoner = BedrockReportReasoner(
                    model_id=model_id,
                    region=self.region,
                    logger=self._reasoning_event,
                    prompt_arn=os.environ.get("SYNTHESIS_PROMPT_ARN"),
                    prompt_default_arn=os.environ.get("SYNTHESIS_PROMPT_DEFAULT_ARN"),
                    prompt_selection_table=selection_table,
                )
                self.planner = BedrockInvestigationPlanner(
                    model_id=model_id,
                    region=self.region,
                    logger=self._planner_event,
                    prompt_arn=os.environ.get("PLANNER_PROMPT_ARN"),
                    prompt_default_arn=os.environ.get("PLANNER_PROMPT_DEFAULT_ARN"),
                    prompt_selection_table=selection_table,
                )

    def _emit(
        self,
        event_type: str,
        step: str,
        status: str,
        payload: dict[str, Any] | None = None,
        *,
        visibility: str = "operator",
    ) -> None:
        workflow_id = self.current_workflow_id
        if not workflow_id:
            return
        table_name = os.environ.get("WORKFLOW_EVENTS_TABLE")
        if not table_name:
            return
        try:
            self.dynamodb.Table(table_name).put_item(
                Item={
                    "WorkflowId": workflow_id,
                    "EventId": f"{time.time_ns()}#{uuid4().hex}",
                    "TenantId": self.current_tenant_id if hasattr(self, "current_tenant_id") else "tenant_acme",
                    "EventType": event_type,
                    "Step": step,
                    "Status": status,
                    "Visibility": visibility,
                    "Payload": json.dumps(payload or {}, sort_keys=True, default=str)[:24000],
                    "CreatedAt": datetime.now(timezone.utc).isoformat(),
                    "ExpiresAtEpoch": int(time.time()) + (30 * 24 * 60 * 60),
                }
            )
        except Exception as exc:
            # Observability must not turn a successful investigation into a failure.
            print(json.dumps({"event": "workflow_event_degraded", "error": str(exc)}))

    def _planner_event(self, event: dict[str, Any]) -> None:
        # Keep the prompt version and bounded dynamic variables in the admin
        # audit stream, but make only the plan summary available to operators.
        self._emit("llm_plan", "planning", "completed", event, visibility="admin")
        self._emit(
            "reasoning_plan",
            "planning",
            "completed",
            {
                "model_id": event.get("model_id"),
                "plan": event.get("plan", {}),
                "prompt_redacted": True,
            },
            visibility="operator",
        )

    def _reasoning_event(self, event: dict[str, Any]) -> None:
        # The raw model prompt remains admin-only. Operators see lifecycle
        # state and model metadata, never private chain-of-thought or prompts.
        event_name = str(event.get("event", ""))
        status = "started" if event_name.endswith("_started") else "completed"
        self._emit("llm_synthesis", "report_synthesis", status, event, visibility="admin")
        self._emit(
            "reasoning_synthesis",
            "report_synthesis",
            status,
            {
                "model_id": event.get("model_id"),
                "stop_reason": event.get("stop_reason"),
                "prompt_redacted": True,
            },
            visibility="operator",
        )

    def _checkpoint(self, workflow_id: str, customer_id: str, step: str, data: dict[str, Any]) -> None:
        table_name = os.environ.get("WORKFLOW_CHECKPOINTS_TABLE")
        if not table_name:
            return
        table = self.dynamodb.Table(table_name)
        table.update_item(
            Key={"WorkflowId": workflow_id},
            UpdateExpression="SET CustomerId = :customer, TenantId = :tenant, CurrentStep = :step, #data = :data, UpdatedAt = :updated",
            ExpressionAttributeNames={"#data": "Data"},
            ExpressionAttributeValues={
                ":customer": customer_id,
                ":tenant": self.current_tenant_id if hasattr(self, "current_tenant_id") else "tenant_acme",
                ":step": step,
                ":data": json.dumps(data, sort_keys=True),
                ":updated": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def _items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return [item for item in value["items"] if isinstance(item, dict)]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _clip_preview(value: Any, limit: int = 320) -> Any:
        if isinstance(value, str):
            return value if len(value) <= limit else f"{value[: limit - 1]}…"
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)

    @classmethod
    def _result_preview(cls, tool: str, items: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
        """Return bounded, operator-safe record previews for the execution log.

        The event stream should show what a read-only tool returned without
        copying an unbounded DynamoDB row, a prompt, or an entire vector body.
        Fields are allow-listed per tool and the caller already holds the
        tenant-scoped result set returned by Gateway.
        """

        preview: list[dict[str, Any]] = []
        fields = {
            "search_incidents": ("IncidentId", "CustomerId", "Category", "Severity", "Description", "DeploymentVersion", "Status", "CreatedAt"),
            "get_customer_invoices": ("InvoiceId", "CustomerId", "RenewalId", "SubscriptionId", "AmountCents", "Status", "IssuedAt"),
            "get_application_logs": ("LogId", "CustomerId", "Timestamp", "Service", "Event", "PayloadJson"),
            "get_deployments": ("DeploymentId", "Version", "DeployedAt", "Service", "ChangeSummary", "Status"),
        }
        for item in items[:limit]:
            if tool == "search_evidence":
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                record: dict[str, Any] = {
                    "id": metadata.get("source_id") or item.get("key"),
                    "title": cls._clip_preview(metadata.get("title"), 180),
                    "source_type": metadata.get("source_type"),
                    "version": metadata.get("version"),
                    "observed_at": metadata.get("observed_at"),
                    "status": metadata.get("status"),
                    "snippet": cls._clip_preview(metadata.get("text"), 320),
                }
                scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
                if scores:
                    record["scores"] = {key: scores[key] for key in ("fused", "vector", "text") if key in scores}
                if item.get("application_rerank_score") is not None:
                    record["application_rerank_score"] = item.get("application_rerank_score")
                preview.append({key: value for key, value in record.items() if value not in (None, "", {})})
                continue
            if tool == "memory_lookup":
                content = item.get("content") if isinstance(item.get("content"), dict) else {}
                record = {
                    "memory_id": item.get("memoryRecordId") or item.get("memory_id") or item.get("id"),
                    "score": item.get("score"),
                    "text": cls._clip_preview(content.get("text") or item.get("text") or item.get("value"), 320),
                    "namespace": item.get("namespace") or item.get("namespacePath"),
                    "created_at": item.get("createdAt") or item.get("created_at"),
                }
                preview.append({key: value for key, value in record.items() if value not in (None, "", {})})
                continue
            record = {}
            for field in fields.get(tool, ("IncidentId", "InvoiceId", "LogId", "DeploymentId", "Status", "Version")):
                if field not in item:
                    continue
                value = item[field]
                if field == "PayloadJson" and isinstance(value, dict):
                    allowed_payload = (
                        "payment_id", "idempotency_key", "original_idempotency_key", "retry_idempotency_key",
                        "renewal_id", "invoice_id", "error", "attempt", "provider_status", "amount_cents", "merchant_profile",
                    )
                    value = {key: cls._clip_preview(value[key]) for key in allowed_payload if key in value}
                elif field in {"Description", "ChangeSummary"}:
                    value = cls._clip_preview(value, 320)
                else:
                    value = cls._clip_preview(value)
                record[field] = value
            preview.append(record)
        return preview

    def _call(self, target: str, tool: str, tenant_id: str, **arguments: Any) -> list[dict[str, Any]]:
        payload = {"tenant_id": tenant_id, **arguments}
        self._emit("tool_call", tool, "started", {"target": target, "tool": tool, "arguments": payload})
        started = time.perf_counter()
        try:
            items = self._items(self.gateway.call_tool(f"{target}___{tool}", payload))
        except Exception as exc:
            self._emit(
                "tool_call",
                tool,
                "failed",
                {"target": target, "tool": tool, "error": str(exc), "latency_ms": round((time.perf_counter() - started) * 1000, 2)},
            )
            raise
        self._emit(
            "tool_call",
            tool,
            "completed",
            {
                "target": target,
                "tool": tool,
                "result_count": len(items),
                "result_preview": self._result_preview(tool, items),
                "preview_truncated": len(items) > 4,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return items

    @staticmethod
    def _default_plan(payload: dict[str, Any]) -> InvestigationPlan:
        request = str(payload.get("request") or "").strip()
        return InvestigationPlan(
            goal="Investigate the reported customer incident.",
            incident_query=str(payload.get("incident_id") or payload.get("customer_id") or ""),
            evidence_query=request or "incident evidence",
            deployment_version=None,
            include_invoices=True,
            include_logs=True,
            include_deployments=True,
            rationale="Use the standard read-only investigation path.",
        )

    def _plan(self, payload: dict[str, Any]) -> InvestigationPlan:
        if self.planner is None:
            plan = self._default_plan(payload)
            self._emit(
                "reasoning_plan",
                "planning",
                "skipped",
                {"reason": "model_reasoning_disabled", "plan": plan.to_dict(), "prompt_redacted": True},
            )
            return plan
        try:
            self._emit("llm_plan", "planning", "started", {"model_id": self.planner.model_id, "request": payload.get("request", "")}, visibility="admin")
            self._emit(
                "reasoning_plan",
                "planning",
                "started",
                {"model_id": self.planner.model_id, "prompt_redacted": True},
            )
            plan = self.planner.plan(payload)
            self._emit("llm_plan", "planning", "completed", {"model_id": self.planner.model_id, "plan": plan.to_dict()}, visibility="admin")
            return plan
        except Exception as exc:
            plan = self._default_plan(payload)
            self._emit("llm_plan", "planning", "degraded", {"error": str(exc), "plan": plan.to_dict()}, visibility="admin")
            self._emit(
                "reasoning_plan",
                "planning",
                "degraded",
                {"reason": "model_planning_unavailable", "fallback_used": True, "plan": plan.to_dict(), "prompt_redacted": True},
            )
            return plan

    def _memory_records(self, tenant_id: str, subject_id: str, query: str) -> list[dict[str, Any]]:
        memory_id = os.environ.get("AGENTCORE_MEMORY_ID")
        if not memory_id:
            return []
        try:
            response = self.memory.retrieve_memory_records(
                memoryId=memory_id,
                namespacePath=f"/verified/{tenant_id}/{subject_id}/",
                searchCriteria={"searchQuery": query, "topK": 5},
            )
            return list(response.get("memoryRecordSummaries", []))
        except Exception as exc:
            # Memory should enrich an investigation; a transient memory read
            # must not hide primary operational evidence.
            print(json.dumps({"event": "memory_read_degraded", "error": str(exc)}))
            return []

    def _record_memory_event(self, workflow_id: str, tenant_id: str, report: IncidentReport) -> None:
        memory_id = os.environ.get("AGENTCORE_MEMORY_ID")
        if not memory_id:
            return
        try:
            self.memory.create_event(
                memoryId=memory_id,
                actorId=f"tenant:{tenant_id}",
                sessionId=workflow_id,
                eventTimestamp=datetime.now(timezone.utc),
                payload=[{"json": {"content": {"incident_id": report.incident_id, "root_cause": report.root_cause, "evidence_ids": report.evidence_ids}}}],
                metadata={"tenant_id": {"stringValue": tenant_id}, "memory_kind": {"stringValue": "incident_report"}},
                extractionConfig={"namespaceVariables": {"tenantid": tenant_id, "subjectid": report.customer_id}},
            )
        except Exception as exc:
            print(json.dumps({"event": "memory_write_degraded", "error": str(exc)}))

    def _publish_idempotently(self, workflow_id: str, report: IncidentReport) -> IncidentReport:
        table_name = os.environ.get("OPERATION_LEDGER_TABLE")
        if not table_name:
            return report
        table = self.dynamodb.Table(table_name)
        operation_id = f"report:{workflow_id}"
        try:
            table.put_item(
                Item={"OperationId": operation_id, "WorkflowId": workflow_id, "OperationType": "publish_report", "Status": "STARTED"},
                ConditionExpression="attribute_not_exists(OperationId)",
            )
        except Exception as exc:
            try:
                from botocore.exceptions import ClientError
            except ImportError:
                ClientError = ()  # type: ignore[assignment]
            if ClientError and isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            existing = table.get_item(Key={"OperationId": operation_id}).get("Item", {})
            if existing.get("Status") == "SUCCEEDED" and existing.get("ResultJson"):
                return IncidentReport(**json.loads(existing["ResultJson"]))
            # A prior worker may have died after the write began. Reusing the
            # same operation ID makes the subsequent completion deterministic.
        table.update_item(
            Key={"OperationId": operation_id},
            UpdateExpression="SET #status = :status, ResultJson = :result, CompletedAt = :completed",
            ExpressionAttributeNames={"#status": "Status"},
            ExpressionAttributeValues={
                ":status": "SUCCEEDED",
                ":result": json.dumps(report.to_dict(), sort_keys=True),
                ":completed": datetime.now(timezone.utc).isoformat(),
            },
        )
        return report

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        customer_id = str(payload["customer_id"])
        tenant_id = str(payload.get("tenant_id", "tenant_acme"))
        workflow_id = str(payload.get("workflow_id") or f"runtime-{customer_id}")
        self.current_workflow_id = workflow_id
        self.current_tenant_id = tenant_id
        self._emit("workflow", "investigation", "started", {"customer_id": customer_id, "tenant_id": tenant_id, "request": payload.get("request", "")})
        self._checkpoint(workflow_id, customer_id, "started", {})

        plan = self._plan(payload)
        incidents = self._call("Incidents", "search_incidents", tenant_id, query=plan.incident_query)
        if not incidents:
            incidents = self._call("Incidents", "search_incidents", tenant_id, query=customer_id)
        if not incidents:
            raise ValueError(f"no incident found for {customer_id}")
        incident = incidents[0]
        self._checkpoint(workflow_id, customer_id, "incident_lookup", {"incident_id": incident.get("IncidentId")})

        invoices = self._call("Invoices", "get_customer_invoices", tenant_id, customer_id=customer_id) if plan.include_invoices else []
        self._checkpoint(workflow_id, customer_id, "invoice_lookup", {"count": len(invoices)})
        logs = self._call("ApplicationLogs", "get_application_logs", tenant_id, customer_id=customer_id, limit=100) if plan.include_logs else []
        self._checkpoint(workflow_id, customer_id, "log_lookup", {"count": len(logs)})
        deployment_version = plan.deployment_version or incident.get("DeploymentVersion", "3.8.2")
        deployments = self._call("Deployments", "get_deployments", tenant_id, version=deployment_version) if plan.include_deployments else []
        self._checkpoint(workflow_id, customer_id, "deployment_lookup", {"count": len(deployments)})
        vector_items = self._call(
            "Evidence",
            "search_evidence",
            tenant_id,
            query=plan.evidence_query,
            top_k=6,
        )
        self._checkpoint(workflow_id, customer_id, "retrieval", {"count": len(vector_items)})
        memories = self._memory_records(tenant_id, "payment-retry-handler", "payment retry idempotency policy")
        self._emit(
            "memory",
            "memory_lookup",
            "completed",
            {
                "result_count": len(memories),
                "result_preview": self._result_preview("memory_lookup", memories),
                "preview_truncated": len(memories) > 4,
            },
        )
        self._checkpoint(workflow_id, customer_id, "memory_lookup", {"count": len(memories)})

        report = self._build_report(incident, invoices, logs, deployments, vector_items, memories, tenant_id)
        if self.reasoner is not None:
            packet = self._evidence_packet(incident, invoices, logs, deployments, vector_items)
            try:
                report = self.reasoner.synthesize(report, evidence=packet, memories=memories)
            except ModelReasoningError as exc:
                print(json.dumps({"event": "model_reasoning_degraded", "error": str(exc)}))
                self._emit(
                    "reasoning_synthesis",
                    "report_synthesis",
                    "degraded",
                    {"reason": "model_synthesis_unavailable", "fallback_used": True, "prompt_redacted": True},
                )
        report = self._publish_idempotently(workflow_id, report)
        self._checkpoint(workflow_id, customer_id, "completed", {"report": report.to_dict()})
        self._record_memory_event(workflow_id, tenant_id, report)
        reports_bucket = os.environ.get("REPORTS_BUCKET")
        if reports_bucket:
            self.s3.put_object(
                Bucket=reports_bucket,
                Key=f"investigations/{workflow_id}.json",
                Body=json.dumps(report.to_dict(), sort_keys=True).encode("utf-8"),
                ContentType="application/json",
            )
        self._emit("workflow", "investigation", "completed", {"report": report.to_dict()})
        return report.to_dict()

    @staticmethod
    def _evidence_packet(
        incident: dict[str, Any],
        invoices: list[dict[str, Any]],
        logs: list[dict[str, Any]],
        deployments: list[dict[str, Any]],
        vectors: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        packet: list[dict[str, Any]] = []
        for kind, items, identifier in (
            ("incident", [incident], "IncidentId"),
            ("invoice", invoices, "InvoiceId"),
            ("application_log", logs, "LogId"),
            ("deployment", deployments, "DeploymentId"),
        ):
            for item in items:
                packet.append({"id": item.get(identifier), "kind": kind, "data": item})
        for item in vectors:
            metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            packet.append({
                "id": metadata.get("document_id") or str(item.get("key", "")).split("#", 1)[0],
                "kind": "vector_evidence",
                "data": item,
            })
        return packet

    @staticmethod
    def _build_report(
        incident: dict[str, Any],
        invoices: list[dict[str, Any]],
        logs: list[dict[str, Any]],
        deployments: list[dict[str, Any]],
        vectors: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        tenant_id: str,
    ) -> IncidentReport:
        return build_evidence_report(
            incident=incident,
            invoices=invoices,
            logs=logs,
            deployments=deployments,
            vectors=vectors,
            memories=memories,
            tenant_id=tenant_id,
            task_type="investigation",
        )
