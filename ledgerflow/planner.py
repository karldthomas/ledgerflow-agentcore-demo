"""Bounded Bedrock planning for the LedgerFlow investigation workflow."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from .prompting import PromptVersionResolver


@dataclass(frozen=True)
class InvestigationPlan:
    """The small, validated plan the model is allowed to choose."""

    goal: str
    incident_query: str
    evidence_query: str
    deployment_version: str | None
    include_invoices: bool
    include_logs: bool
    include_deployments: bool
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "incident_query": self.incident_query,
            "evidence_query": self.evidence_query,
            "deployment_version": self.deployment_version,
            "include_invoices": self.include_invoices,
            "include_logs": self.include_logs,
            "include_deployments": self.include_deployments,
            "rationale": self.rationale,
        }


class BedrockInvestigationPlanner:
    """Use Bedrock to refine an investigation without granting tool authority."""

    def __init__(
        self,
        *,
        model_id: str,
        region: str | None = None,
        client: Any | None = None,
        logger: Callable[[dict[str, Any]], None] | None = None,
        prompt_arn: str | None = None,
        prompt_default_arn: str | None = None,
        prompt_selection_table: Any | None = None,
        prompt_key: str = "planning",
    ) -> None:
        self.model_id = model_id
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
            raise RuntimeError("boto3 is required for Bedrock planning") from exc
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )

    @staticmethod
    def _parse(text: str) -> dict[str, Any]:
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
                raise ValueError("planner response did not contain JSON")
            value = json.loads(candidate[start : end + 1])
        if not isinstance(value, dict):
            raise ValueError("planner response must be a JSON object")
        return value

    @staticmethod
    def _text(value: Any, fallback: str, limit: int = 800) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()[:limit]
        return fallback

    @staticmethod
    def _bool(value: Any, fallback: bool = True) -> bool:
        return value if isinstance(value, bool) else fallback

    def plan(self, payload: dict[str, Any]) -> InvestigationPlan:
        user = json.dumps(
            {
                "customer_id": str(payload.get("customer_id", "")),
                "incident_id": str(payload.get("incident_id", "")),
                "request": str(payload.get("request", "")),
                "allowed_output": {
                    "goal": "short investigation goal",
                    "incident_query": "incident search text",
                    "evidence_query": "hybrid evidence search text",
                    "deployment_version": "version string or null",
                    "include_invoices": "boolean",
                    "include_logs": "boolean",
                    "include_deployments": "boolean",
                    "rationale": "short explanation",
                },
            },
            sort_keys=True,
        )
        prompt_version_arn = self.prompt_resolver.resolve()
        response = self.client.converse(
            modelId=prompt_version_arn,
            promptVariables={"input": {"text": user}},
        )
        content = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("text"))
        candidate = self._parse(text)
        incident_default = str(payload.get("incident_id") or payload.get("customer_id") or "").strip()
        evidence_default = "duplicate invoice timeout retry idempotency key"
        version = candidate.get("deployment_version")
        if not isinstance(version, str) or not version.strip():
            version = None
        plan = InvestigationPlan(
            goal=self._text(candidate.get("goal"), "Investigate the reported customer incident."),
            incident_query=self._text(candidate.get("incident_query"), incident_default),
            evidence_query=self._text(candidate.get("evidence_query"), evidence_default),
            deployment_version=version.strip() if version else None,
            include_invoices=self._bool(candidate.get("include_invoices")),
            include_logs=self._bool(candidate.get("include_logs")),
            include_deployments=self._bool(candidate.get("include_deployments")),
            rationale=self._text(candidate.get("rationale"), "Use the available read-only evidence sources."),
        )
        self.logger({
            "event": "llm_plan",
            "model_id": self.model_id,
            "prompt_version_arn": prompt_version_arn,
            "prompt_variables_preview": user[:6000],
            "plan": plan.to_dict(),
        })
        return plan
