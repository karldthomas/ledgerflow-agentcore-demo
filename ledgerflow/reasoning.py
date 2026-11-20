"""Bedrock report synthesis with a strict evidence boundary.

When ``MODEL_REASONING_ENABLED=ENABLED`` and ``MODEL_ID`` is set, this module
uses Bedrock Converse to validate and enrich structured citations for a report
already grounded by the read-only workflow. The deterministic evidence report
remains authoritative for all user-visible narrative fields. The model never
chooses tools, tenants, permissions, or durable state.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .context_compaction import (
    CompactedContext,
    ContextCompactor,
    ContextInvariantError,
    validate_context,
)
from .models import IncidentReport
from .prompting import PromptVersionResolver


class ModelReasoningError(RuntimeError):
    """Raised when a model response cannot be converted into a safe report."""


def _safe_claims(value: Any, allowed_ids: set[str], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep model claims structured and grounded in the evidence packet."""

    if not isinstance(value, list):
        return list(fallback)
    claims: list[dict[str, Any]] = []
    allowed_types = {"fact", "relationship", "hypothesis"}
    for item in value:
        if not isinstance(item, dict):
            continue
        claim_type = str(item.get("type", "")).strip().lower()
        statement = str(item.get("statement", "")).strip()
        ids = [str(identifier) for identifier in item.get("evidence_ids", []) if str(identifier) in allowed_ids]
        if claim_type not in allowed_types or not statement or not ids:
            continue
        claim: dict[str, Any] = {
            "type": claim_type,
            "statement": statement[:1200],
            "evidence_ids": list(dict.fromkeys(ids)),
        }
        try:
            if "confidence" in item:
                claim["confidence"] = min(1.0, max(0.0, float(item["confidence"])))
        except (TypeError, ValueError):
            pass
        claims.append(claim)
    return claims or list(fallback)


def _parse_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").strip()
        if candidate.startswith("json"):
            candidate = candidate[4:].lstrip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise ModelReasoningError("Bedrock response did not contain a JSON object")
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelReasoningError("Bedrock response contained invalid report JSON") from exc
    if not isinstance(value, dict):
        raise ModelReasoningError("Bedrock report response must be a JSON object")
    return value


class BedrockReportReasoner:
    """Synthesize a report from evidence already collected by the workflow."""

    def __init__(
        self,
        *,
        model_id: str,
        region: str | None = None,
        client: Any | None = None,
        max_tokens: int = 1400,
        logger: Callable[[dict[str, Any]], None] | None = None,
        prompt_arn: str | None = None,
        prompt_default_arn: str | None = None,
        prompt_selection_table: Any | None = None,
        prompt_key: str = "synthesis",
        context_token_budget: int = 8000,
    ) -> None:
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.context_compactor = ContextCompactor(max_tokens=context_token_budget)
        self.prompt_resolver = PromptVersionResolver(
            prompt_arn=prompt_arn,
            default_version_arn=prompt_default_arn,
            selection_table=prompt_selection_table,
            prompt_key=prompt_key,
        )
        self.logger = logger or (lambda event: print(json.dumps(event, sort_keys=True)))
        if client is not None:
            self.client = client
            return
        try:
            import boto3
        except ImportError as exc:
            raise ModelReasoningError("boto3 is required for Bedrock report synthesis") from exc
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )

    @staticmethod
    def _content_text(response: dict[str, Any]) -> str:
        content = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("text"))
        if not text:
            raise ModelReasoningError("Bedrock response did not contain message text")
        return text

    def build_context(
        self,
        base_report: IncidentReport,
        *,
        evidence: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        workflow_context: dict[str, Any] | None = None,
    ) -> CompactedContext:
        """Build the validated context used by the synthesis prompt."""

        tenant_id = str((workflow_context or {}).get("tenant_id") or "unknown")
        return self.context_compactor.compact(
            base_report=base_report,
            evidence=evidence,
            memories=memories,
            tenant_id=tenant_id,
            workflow_context=workflow_context,
        )

    def synthesize(
        self,
        base_report: IncidentReport,
        *,
        evidence: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        context: CompactedContext | None = None,
        workflow_context: dict[str, Any] | None = None,
    ) -> IncidentReport:
        try:
            context = context or self.build_context(
                base_report,
                evidence=evidence,
                memories=memories,
                workflow_context=workflow_context,
            )
            validate_context(context)
        except ContextInvariantError as exc:
            raise ModelReasoningError(f"context compaction failed: {exc}") from exc

        allowed_ids = set(context.citable_evidence_ids)
        user = json.dumps(
            {
                "investigation_context": context.to_prompt_dict(),
                "allowed_evidence_ids": sorted(allowed_ids),
            },
            sort_keys=True,
        )
        prompt_version_arn = self.prompt_resolver.resolve()
        self.logger({
            "event": "llm_synthesis_started",
            "model_id": self.model_id,
            "prompt_version_arn": prompt_version_arn,
            "context_schema_version": context.schema_version,
            "compaction_version": context.compaction_version,
            "context_digest": context.digest,
            "input_token_count": context.token_count,
            "omitted_evidence_count": context.omitted_evidence_count,
            "prompt_variables_preview": user[:6000],
        })
        try:
            response = self.client.converse(
                modelId=prompt_version_arn,
                promptVariables={"input": {"text": user}},
            )
            self.logger({
                "event": "llm_synthesis_completed",
                "model_id": self.model_id,
                "prompt_version_arn": prompt_version_arn,
                "stop_reason": response.get("stopReason"),
            })
            candidate = _parse_json(self._content_text(response))
        except Exception as exc:
            raise ModelReasoningError(str(exc)) from exc

        candidate_ids = [str(item) for item in candidate.get("evidence_ids", []) if str(item) in allowed_ids]
        evidence_ids = list(base_report.evidence_ids)
        evidence_ids.extend(item for item in candidate_ids if item not in evidence_ids)
        confidence_value = candidate.get("confidence", base_report.confidence)
        try:
            confidence = min(1.0, max(0.0, float(confidence_value)))
        except (TypeError, ValueError):
            confidence = base_report.confidence
        return IncidentReport(
            incident_id=base_report.incident_id,
            customer_id=base_report.customer_id,
            evidence_ids=evidence_ids,
            confidence=confidence,
            # Narrative fields stay deterministic until the model supplies a
            # field-level claim envelope. A single top-level evidence_ids list
            # cannot prove which sentence or recommendation it supports.
            root_cause=base_report.root_cause or "Evidence was insufficient.",
            recommended_actions=list(base_report.recommended_actions),
            unresolved_questions=list(base_report.unresolved_questions),
            executive_summary=base_report.executive_summary,
            affected_scope=base_report.affected_scope,
            incident_timeline=list(base_report.incident_timeline),
            verified_findings=list(base_report.verified_findings),
            recovery_history=base_report.recovery_history,
            remaining_risks=list(base_report.remaining_risks),
            task_type=base_report.task_type,
            evidence_schema_version=base_report.evidence_schema_version,
            evidence_claims=_safe_claims(candidate.get("evidence_claims"), allowed_ids, base_report.evidence_claims),
        )
