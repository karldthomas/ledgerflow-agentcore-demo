"""AWS-backed investigation handler used by the Runtime container.

The handler keeps the shared report contract while moving business-data access
behind AgentCore Gateway, checkpoints into DynamoDB, and long-term memory into
AgentCore Memory.
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


WORKFLOW_STATE_SCHEMA_VERSION = "ledgerflow.workflow.v1"
WORKFLOW_LEASE_SECONDS = 60
DEV_TOOLS_ENVIRONMENTS = {"dev", "test"}


class WorkflowInProgressError(RuntimeError):
    """Raised when another worker currently owns a workflow lease."""


class DeterministicFailure(RuntimeError):
    """Raised by the opt-in one-shot failure injector after a durable checkpoint."""


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
        self.worker_id = f"{os.environ.get('HOSTNAME', 'ledgerflow-runtime')}:{os.getpid()}:{uuid4().hex}"
        try:
            self.lease_seconds = max(5, int(os.environ.get("WORKFLOW_LEASE_SECONDS", str(WORKFLOW_LEASE_SECONDS))))
        except ValueError:
            self.lease_seconds = WORKFLOW_LEASE_SECONDS
        self.current_workflow_id: str | None = None
        self.current_tenant_id: str | None = None
        self.failure_injection_consumed = False
        self.resumed = False
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

    @staticmethod
    def _is_conditional_failure(exc: Exception) -> bool:
        response = getattr(exc, "response", {})
        if isinstance(response, dict):
            error = response.get("Error", {})
            if isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException":
                return True
        return "ConditionalCheckFailedException" in str(exc)

    def _load_checkpoint(self, workflow_id: str) -> dict[str, Any] | None:
        table_name = os.environ.get("WORKFLOW_CHECKPOINTS_TABLE")
        if not table_name:
            return None
        item = self.dynamodb.Table(table_name).get_item(
            Key={"WorkflowId": workflow_id},
            ConsistentRead=True,
        ).get("Item")
        if not item:
            return None
        raw_data = item.get("Data", "{}")
        try:
            state = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        except (TypeError, json.JSONDecodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        schema_version = str(item.get("StateSchemaVersion", ""))
        return {
            "item": item,
            "state": state if schema_version == WORKFLOW_STATE_SCHEMA_VERSION else {},
            "schema_version": schema_version,
            "completed_steps": {
                str(step) for step in item.get("CompletedSteps", []) if isinstance(step, str)
            }
            if schema_version == WORKFLOW_STATE_SCHEMA_VERSION
            else set(),
            "status": str(item.get("Status", "")),
            "failure_injection_consumed": bool(item.get("FailureInjectionConsumed", False)),
        }

    def _acquire_workflow_lease(self, workflow_id: str, customer_id: str, tenant_id: str) -> None:
        table_name = os.environ.get("WORKFLOW_CHECKPOINTS_TABLE")
        if not table_name:
            return
        now = int(time.time())
        owner = getattr(self, "worker_id", "ledgerflow-runtime")
        try:
            self.dynamodb.Table(table_name).update_item(
                Key={"WorkflowId": workflow_id},
                UpdateExpression=(
                    "SET CustomerId = :customer, TenantId = :tenant, "
                    "StateSchemaVersion = :schema, #status = :status, "
                    "LeaseOwner = :owner, LeaseExpiresAt = :expires"
                ),
                ExpressionAttributeNames={"#status": "Status"},
                ExpressionAttributeValues={
                    ":customer": customer_id,
                    ":tenant": tenant_id,
                    ":schema": WORKFLOW_STATE_SCHEMA_VERSION,
                    ":status": "RUNNING",
                    ":owner": owner,
                    ":expires": now + int(getattr(self, "lease_seconds", WORKFLOW_LEASE_SECONDS)),
                    ":now": now,
                },
                ConditionExpression=(
                    "attribute_not_exists(LeaseOwner) OR "
                    "attribute_not_exists(LeaseExpiresAt) OR "
                    "LeaseExpiresAt < :now OR LeaseOwner = :owner"
                ),
            )
        except Exception as exc:
            if self._is_conditional_failure(exc):
                raise WorkflowInProgressError(f"workflow {workflow_id} is leased by another worker") from exc
            raise

    def _release_workflow_lease(self, workflow_id: str) -> None:
        table_name = os.environ.get("WORKFLOW_CHECKPOINTS_TABLE")
        if not table_name:
            return
        try:
            self.dynamodb.Table(table_name).update_item(
                Key={"WorkflowId": workflow_id},
                UpdateExpression="REMOVE LeaseOwner, LeaseExpiresAt",
                ExpressionAttributeValues={":owner": getattr(self, "worker_id", "ledgerflow-runtime")},
                ConditionExpression="LeaseOwner = :owner",
            )
        except Exception as exc:
            if not self._is_conditional_failure(exc):
                print(json.dumps({"event": "workflow_lease_release_degraded", "error": str(exc)}))

    def _checkpoint(
        self,
        workflow_id: str,
        customer_id: str,
        step: str,
        data: dict[str, Any],
        completed_steps: set[str],
        *,
        failure_after_step: str | None = None,
        status: str = "RUNNING",
    ) -> None:
        table_name = os.environ.get("WORKFLOW_CHECKPOINTS_TABLE")
        inject_failure = bool(
            failure_after_step
            and failure_after_step == step
            and not getattr(self, "failure_injection_consumed", False)
        )
        failure_source = "payload" if inject_failure else ""
        failure_control_id: str | None = None
        if inject_failure:
            self.failure_injection_consumed = True
        if table_name:
            now = datetime.now(timezone.utc).isoformat()
            lease_expires = int(time.time()) + int(getattr(self, "lease_seconds", WORKFLOW_LEASE_SECONDS))
            self.dynamodb.Table(table_name).update_item(
                Key={"WorkflowId": workflow_id},
                UpdateExpression=(
                    "SET CustomerId = :customer, TenantId = :tenant, CurrentStep = :step, "
                    "#data = :data, CompletedSteps = :completed, StateSchemaVersion = :schema, "
                    "#status = :status, CheckpointVersion = if_not_exists(#version, :zero) + :one, "
                    "FailureInjectionConsumed = :failure_consumed, UpdatedAt = :updated, "
                    "LeaseOwner = :owner, LeaseExpiresAt = :lease_expires"
                ),
                ExpressionAttributeNames={"#data": "Data", "#status": "Status", "#version": "CheckpointVersion"},
                ExpressionAttributeValues={
                    ":customer": customer_id,
                    ":tenant": self.current_tenant_id or "tenant_acme",
                    ":step": step,
                    ":data": json.dumps(data, sort_keys=True, default=str),
                    ":completed": sorted(completed_steps),
                    ":schema": WORKFLOW_STATE_SCHEMA_VERSION,
                    ":status": status,
                    ":zero": 0,
                    ":one": 1,
                    ":failure_consumed": bool(getattr(self, "failure_injection_consumed", False)),
                    ":updated": now,
                    ":owner": getattr(self, "worker_id", "ledgerflow-runtime"),
                    ":lease_expires": lease_expires,
                    ":lease_now": int(time.time()),
                },
                ConditionExpression="LeaseOwner = :owner AND LeaseExpiresAt >= :lease_now",
            )
            self._emit(
                "checkpoint",
                step,
                "completed",
                {
                    "completed_steps": sorted(completed_steps),
                    "resumed": bool(getattr(self, "resumed", False)),
                    "failure_injection_consumed": bool(getattr(self, "failure_injection_consumed", False)),
                },
            )
            if not inject_failure:
                dynamic_plan = self._claim_dynamic_failure(workflow_id, step)
                if dynamic_plan:
                    inject_failure = True
                    failure_source = "dev_tools"
                    failure_control_id = str(dynamic_plan.get("ControlId") or "")
                    self.failure_injection_consumed = True
        if inject_failure:
            self._emit(
                "failure_injection",
                step,
                "failed",
                {
                    "failure_after_step": step,
                    "checkpoint_durable": bool(table_name),
                    "source": failure_source or "payload",
                    **({"control_id": failure_control_id} if failure_control_id else {}),
                },
            )
            # This is a controlled crash simulation. Releasing the lease makes
            # the next Step Functions retry deterministic; an actual process
            # crash leaves the lease to expire normally.
            self._release_workflow_lease(workflow_id)
            raise DeterministicFailure(f"deterministic failure injected after checkpoint {step}")

    def _claim_dynamic_failure(self, workflow_id: str, step: str) -> dict[str, Any] | None:
        """Atomically claim an admin-armed, one-shot failure plan.

        Dev Tools plans are deliberately available only in non-production
        environments. The query is indexed by workflow and step, while the
        conditional update is the source of truth for the one-shot claim.
        A missing or temporarily unavailable control table fails closed for
        normal investigations; it must never make the runtime unavailable.
        """

        environment = os.environ.get("LEDGERFLOW_ENVIRONMENT", "").strip().lower()
        table_name = os.environ.get("DEV_TOOLS_CONTROLS_TABLE", "").strip()
        if step == "completed" or environment not in DEV_TOOLS_ENVIRONMENTS or not table_name:
            return None
        try:
            from boto3.dynamodb.conditions import Key

            table = self.dynamodb.Table(table_name)
            response = table.query(
                IndexName="WorkflowStepIndex",
                KeyConditionExpression=Key("WorkflowId").eq(workflow_id) & Key("Step").eq(step),
                ScanIndexForward=False,
                Limit=20,
            )
        except Exception as exc:
            print(json.dumps({"event": "dev_tools_failure_plan_degraded", "error": str(exc)}))
            return None

        now = int(time.time())
        candidates = sorted(
            [
                item
                for item in response.get("Items", [])
                if isinstance(item, dict)
                and str(item.get("WorkflowId") or "") == workflow_id
                and str(item.get("Step") or "") == step
            ],
            key=lambda item: str(item.get("CreatedAt") or ""),
            reverse=True,
        )
        for item in candidates:
            control_id = str(item.get("ControlId") or "")
            if not control_id or not item.get("Enabled", False) or item.get("Consumed", False):
                continue
            try:
                expires_at = int(item.get("ExpiresAtEpoch", 0))
            except (TypeError, ValueError):
                continue
            if expires_at <= now:
                continue
            try:
                table.update_item(
                    Key={"ControlId": control_id},
                    UpdateExpression="SET Consumed = :true, ConsumedAt = :updated, ConsumedBy = :worker, UpdatedAt = :updated",
                    ConditionExpression=(
                        "Enabled = :enabled AND "
                        "(attribute_not_exists(Consumed) OR Consumed = :false) AND "
                        "ExpiresAtEpoch > :now"
                    ),
                    ExpressionAttributeValues={
                        ":true": True,
                        ":false": False,
                        ":enabled": True,
                        ":now": now,
                        ":worker": self.worker_id,
                        ":updated": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception as exc:
                if self._is_conditional_failure(exc):
                    continue
                print(json.dumps({"event": "dev_tools_failure_plan_claim_degraded", "error": str(exc)}))
                continue
            return item
        return None

    @staticmethod
    def _failure_after_step(payload: dict[str, Any]) -> str | None:
        value = str(payload.get("failure_after_step") or "").strip()
        if not value:
            return None
        if os.environ.get("LEDGERFLOW_ENABLE_FAILURE_INJECTION", "DISABLED").upper() != "ENABLED":
            raise ValueError("failure injection is disabled")
        valid_steps = {
            "started",
            "planning",
            "incident_lookup",
            "invoice_lookup",
            "log_lookup",
            "deployment_lookup",
            "retrieval",
            "memory_lookup",
            "report_synthesis",
            "completed",
        }
        if value not in valid_steps:
            raise ValueError(f"failure_after_step must be one of: {', '.join(sorted(valid_steps))}")
        if not os.environ.get("WORKFLOW_CHECKPOINTS_TABLE"):
            raise ValueError("failure injection requires WORKFLOW_CHECKPOINTS_TABLE")
        return value

    def _save_stage(
        self,
        *,
        workflow_id: str,
        customer_id: str,
        step: str,
        state: dict[str, Any],
        completed_steps: set[str],
        failure_after_step: str | None,
    ) -> None:
        completed_steps.add(step)
        self._checkpoint(
            workflow_id,
            customer_id,
            step,
            state,
            completed_steps,
            failure_after_step=failure_after_step,
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
        failure_after_step = self._failure_after_step(payload)
        self.current_workflow_id = workflow_id
        self.current_tenant_id = tenant_id
        checkpoint = self._load_checkpoint(workflow_id)
        if checkpoint:
            item = checkpoint["item"]
            stored_customer = str(item.get("CustomerId", ""))
            stored_tenant = str(item.get("TenantId", ""))
            if stored_customer and stored_customer != customer_id:
                raise ValueError(f"workflow {workflow_id} belongs to another customer")
            if stored_tenant and stored_tenant != tenant_id:
                raise ValueError(f"workflow {workflow_id} belongs to another tenant")
            if checkpoint["status"] == "COMPLETED" and checkpoint["state"].get("report"):
                report = IncidentReport(**checkpoint["state"]["report"])
                self._emit("workflow", "investigation", "reused", {"reason": "completed_checkpoint"})
                return report.to_dict()

        self.failure_injection_consumed = bool(checkpoint and checkpoint["failure_injection_consumed"])
        self.resumed = bool(checkpoint and checkpoint["completed_steps"])
        self._acquire_workflow_lease(workflow_id, customer_id, tenant_id)

        state = dict(checkpoint["state"]) if checkpoint else {}
        completed_steps = set(checkpoint["completed_steps"]) if checkpoint else set()
        if "started" not in completed_steps:
            state = {}
            completed_steps = set()
            self._emit(
                "workflow",
                "investigation",
                "started",
                {"customer_id": customer_id, "tenant_id": tenant_id, "request": payload.get("request", "")},
            )
            self._save_stage(
                workflow_id=workflow_id,
                customer_id=customer_id,
                step="started",
                state=state,
                completed_steps=completed_steps,
                failure_after_step=failure_after_step,
            )
        else:
            self._emit(
                "workflow",
                "investigation",
                "resumed",
                {
                    "from_step": checkpoint["item"].get("CurrentStep") if checkpoint else "started",
                    "completed_steps": sorted(completed_steps),
                },
            )

        try:
            if "planning" in completed_steps and isinstance(state.get("plan"), dict):
                plan = InvestigationPlan.from_dict(state["plan"])
            else:
                plan = self._plan(payload)
                state["plan"] = plan.to_dict()
                self._save_stage(
                    workflow_id=workflow_id,
                    customer_id=customer_id,
                    step="planning",
                    state=state,
                    completed_steps=completed_steps,
                    failure_after_step=failure_after_step,
                )

            if "incident_lookup" in completed_steps and isinstance(state.get("incident"), dict):
                incident = state["incident"]
            else:
                incidents = self._call("Incidents", "search_incidents", tenant_id, query=plan.incident_query)
                if not incidents:
                    incidents = self._call("Incidents", "search_incidents", tenant_id, query=customer_id)
                if not incidents:
                    raise ValueError(f"no incident found for {customer_id}")
                incident = incidents[0]
                state["incident"] = incident
                self._save_stage(
                    workflow_id=workflow_id,
                    customer_id=customer_id,
                    step="incident_lookup",
                    state=state,
                    completed_steps=completed_steps,
                    failure_after_step=failure_after_step,
                )

            if "invoice_lookup" in completed_steps and isinstance(state.get("invoices"), list):
                invoices = state["invoices"]
            else:
                invoices = self._call("Invoices", "get_customer_invoices", tenant_id, customer_id=customer_id) if plan.include_invoices else []
                state["invoices"] = invoices
                self._save_stage(
                    workflow_id=workflow_id,
                    customer_id=customer_id,
                    step="invoice_lookup",
                    state=state,
                    completed_steps=completed_steps,
                    failure_after_step=failure_after_step,
                )

            if "log_lookup" in completed_steps and isinstance(state.get("logs"), list):
                logs = state["logs"]
            else:
                logs = self._call("ApplicationLogs", "get_application_logs", tenant_id, customer_id=customer_id, limit=100) if plan.include_logs else []
                state["logs"] = logs
                self._save_stage(
                    workflow_id=workflow_id,
                    customer_id=customer_id,
                    step="log_lookup",
                    state=state,
                    completed_steps=completed_steps,
                    failure_after_step=failure_after_step,
                )

            deployment_version = plan.deployment_version or incident.get("DeploymentVersion", "3.8.2")
            if "deployment_lookup" in completed_steps and isinstance(state.get("deployments"), list):
                deployments = state["deployments"]
            else:
                deployments = self._call("Deployments", "get_deployments", tenant_id, version=deployment_version) if plan.include_deployments else []
                state["deployments"] = deployments
                self._save_stage(
                    workflow_id=workflow_id,
                    customer_id=customer_id,
                    step="deployment_lookup",
                    state=state,
                    completed_steps=completed_steps,
                    failure_after_step=failure_after_step,
                )

            if "retrieval" in completed_steps and isinstance(state.get("vector_items"), list):
                vector_items = state["vector_items"]
            else:
                vector_items = self._call(
                    "Evidence",
                    "search_evidence",
                    tenant_id,
                    query=plan.evidence_query,
                    top_k=6,
                )
                state["vector_items"] = vector_items
                self._save_stage(
                    workflow_id=workflow_id,
                    customer_id=customer_id,
                    step="retrieval",
                    state=state,
                    completed_steps=completed_steps,
                    failure_after_step=failure_after_step,
                )

            if "memory_lookup" in completed_steps and isinstance(state.get("memories"), list):
                memories = state["memories"]
            else:
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
                state["memories"] = memories
                self._save_stage(
                    workflow_id=workflow_id,
                    customer_id=customer_id,
                    step="memory_lookup",
                    state=state,
                    completed_steps=completed_steps,
                    failure_after_step=failure_after_step,
                )

            if "report_synthesis" in completed_steps and isinstance(state.get("report"), dict):
                report = IncidentReport(**state["report"])
            else:
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
                state["report"] = report.to_dict()
                self._save_stage(
                    workflow_id=workflow_id,
                    customer_id=customer_id,
                    step="report_synthesis",
                    state=state,
                    completed_steps=completed_steps,
                    failure_after_step=failure_after_step,
                )

            report = self._publish_idempotently(workflow_id, report)
            state["report"] = report.to_dict()
            state["report_published"] = True
            self._checkpoint(
                workflow_id,
                customer_id,
                "report_synthesis",
                state,
                completed_steps,
                failure_after_step=failure_after_step,
            )

            if not state.get("memory_recorded"):
                self._record_memory_event(workflow_id, tenant_id, report)
                state["memory_recorded"] = True
                self._checkpoint(
                    workflow_id,
                    customer_id,
                    "report_synthesis",
                    state,
                    completed_steps,
                    failure_after_step=failure_after_step,
                )

            reports_bucket = os.environ.get("REPORTS_BUCKET")
            if reports_bucket and not state.get("report_stored"):
                self.s3.put_object(
                    Bucket=reports_bucket,
                    Key=f"investigations/{workflow_id}.json",
                    Body=json.dumps(report.to_dict(), sort_keys=True).encode("utf-8"),
                    ContentType="application/json",
                )
                state["report_stored"] = True
                self._checkpoint(
                    workflow_id,
                    customer_id,
                    "report_synthesis",
                    state,
                    completed_steps,
                    failure_after_step=failure_after_step,
                )
            elif not reports_bucket:
                state["report_stored"] = True

            completed_steps.add("completed")
            self._checkpoint(
                workflow_id,
                customer_id,
                "completed",
                state,
                completed_steps,
                failure_after_step=failure_after_step,
                status="COMPLETED",
            )
            self._release_workflow_lease(workflow_id)
            self._emit("workflow", "investigation", "completed", {"report": report.to_dict()})
            return report.to_dict()
        except DeterministicFailure:
            raise
        except Exception:
            self._release_workflow_lease(workflow_id)
            raise

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
