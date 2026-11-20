"""Deterministic context compaction for evidence-grounded investigations.

The compactor reduces the model's working packet; it never becomes the source
of truth for evidence or authorization. Raw tool results remain available to
the workflow, while this module creates a bounded, structured view containing
the records and claims that the model may cite.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping

from .prompting import PromptVersionResolver


CONTEXT_SCHEMA_VERSION = "ledgerflow.context.v1"
COMPACTION_VERSION = "ledgerflow.compaction.v1"

DEFAULT_INVARIANTS: dict[str, Any] = {
    "read_only": True,
    "no_financial_writes": True,
    "require_evidence_for_findings": True,
    "require_approval_for_remediation": True,
    "prohibited_actions": ["refund", "cancel", "delete", "charge", "credit", "modify", "write"],
}


class ContextInvariantError(ValueError):
    """Raised when a compacted context cannot satisfy its safety contract."""


def estimate_tokens(value: Any) -> int:
    """Estimate JSON token usage without adding a model-specific dependency.

    The estimator is deliberately conservative and deterministic. A model
    provider-specific counter can be injected into ``ContextCompactor`` when
    exact token accounting is available.
    """

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return max(1, math.ceil(len(encoded) / 4))


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded(value: Any, *, string_limit: int = 1200, list_limit: int = 20, depth: int = 0) -> Any:
    """Keep untrusted record data bounded without interpreting its meaning."""

    if isinstance(value, str):
        return value if len(value) <= string_limit else f"{value[: string_limit - 1]}…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 5:
        return str(value)[:string_limit]
    if isinstance(value, Mapping):
        return {
            str(key): _bounded(item, string_limit=string_limit, list_limit=list_limit, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded(item, string_limit=string_limit, list_limit=list_limit, depth=depth + 1)
            for item in list(value)[:list_limit]
        ]
    return str(value)[:string_limit]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _first_present(value: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if value.get(key) not in (None, ""):
            return value[key]
    return None


def _tenant_from(value: Mapping[str, Any]) -> str | None:
    direct = _first_present(value, ("TenantId", "tenant_id", "tenantId"))
    if direct not in (None, ""):
        return str(direct)
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping):
        nested = _first_present(metadata, ("TenantId", "tenant_id", "tenantId"))
        if nested not in (None, ""):
            return str(nested)
    return None


def _evidence_id(item: Mapping[str, Any]) -> str | None:
    direct = item.get("id") or item.get("evidence_id")
    data = item.get("data") if isinstance(item.get("data"), Mapping) else item
    metadata = data.get("metadata") if isinstance(data, Mapping) and isinstance(data.get("metadata"), Mapping) else data
    candidate = direct or _first_present(
        metadata if isinstance(metadata, Mapping) else {},
        ("document_id", "source_id", "key", "IncidentId", "InvoiceId", "LogId", "DeploymentId"),
    )
    if candidate in (None, ""):
        return None
    return str(candidate).split("#", 1)[0]


def _memory_id(item: Mapping[str, Any]) -> str:
    candidate = _first_present(item, ("memoryRecordId", "memory_id", "id"))
    if candidate not in (None, ""):
        return str(candidate)
    return f"memory:{_digest(_bounded(item, string_limit=800, list_limit=10))[:16]}"


def _numeric_score(item: Mapping[str, Any]) -> float:
    data = item.get("data") if isinstance(item.get("data"), Mapping) else item
    scores = data.get("scores") if isinstance(data, Mapping) and isinstance(data.get("scores"), Mapping) else {}
    values = [
        data.get("application_rerank_score") if isinstance(data, Mapping) else None,
        scores.get("fused"),
        scores.get("vector"),
        scores.get("text"),
    ]
    for value in values:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


@dataclass(frozen=True)
class _EvidenceCandidate:
    evidence_id: str
    kind: str
    data: Mapping[str, Any]
    priority: float

    def preview(self, *, reference_only: bool = False) -> dict[str, Any]:
        if reference_only:
            data: dict[str, Any] = {"reference_only": True}
        else:
            data = _bounded(self.data, string_limit=1200, list_limit=16)
        return {"id": self.evidence_id, "kind": self.kind, "data": data}


@dataclass(frozen=True)
class CompactedContext:
    """The bounded context contract passed to Bedrock and its manifest."""

    schema_version: str
    compaction_version: str
    invariants: dict[str, Any]
    workflow: dict[str, Any]
    baseline_report: dict[str, Any]
    facts: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    semantic_summary: dict[str, Any]
    unresolved_questions: list[str]
    failed_hypotheses: list[str]
    evidence: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    citable_evidence_ids: list[str]
    required_evidence_ids: list[str]
    omitted_evidence_count: int
    omitted_evidence_sample: list[str]
    omitted_memory_count: int
    omitted_memory_sample: list[str]
    token_count: int
    budget_tokens: int
    digest: str

    def _evidence_coverage(self) -> dict[str, Any]:
        selected_ids = [str(item.get("id") or "") for item in self.evidence]
        full_ids = [
            str(item.get("id") or "")
            for item in self.evidence
            if not (isinstance(item.get("data"), Mapping) and item["data"].get("reference_only") is True)
        ]
        full_id_set = set(full_ids)
        reference_only_ids = [item_id for item_id in selected_ids if item_id not in full_id_set]
        return {
            "input_evidence_count": len(selected_ids) + self.omitted_evidence_count,
            "selected_evidence_count": len(selected_ids),
            "full_evidence_count": len(full_ids),
            "reference_only_evidence_count": len(reference_only_ids),
            "reference_only_evidence_sample": reference_only_ids[:25],
        }

    def _payload(self, *, include_digest: bool = True) -> dict[str, Any]:
        coverage = self._evidence_coverage()
        return {
            "context_schema_version": self.schema_version,
            "compaction": {
                "version": self.compaction_version,
                "token_count": self.token_count,
                "budget_tokens": self.budget_tokens,
                "digest": self.digest if include_digest else "",
            },
            "invariants": self.invariants,
            "workflow": self.workflow,
            "baseline_report": self.baseline_report,
            "claims": {
                "facts": self.facts,
                "relationships": self.relationships,
                "hypotheses": self.hypotheses,
            },
            "semantic_summary": self.semantic_summary,
            "unresolved_questions": self.unresolved_questions,
            "failed_hypotheses": self.failed_hypotheses,
            "evidence": self.evidence,
            "memories": self.memories,
            "citable_evidence_ids": self.citable_evidence_ids,
            "evidence_coverage": {
                **coverage,
                "required_evidence_ids": self.required_evidence_ids,
                "omitted_evidence_count": self.omitted_evidence_count,
                "omitted_evidence_sample": self.omitted_evidence_sample,
                "omitted_memory_count": self.omitted_memory_count,
                "omitted_memory_sample": self.omitted_memory_sample,
            },
        }

    def to_prompt_dict(self) -> dict[str, Any]:
        return self._payload()

    def with_semantic_summary(
        self,
        summary: dict[str, Any],
        *,
        token_counter: Callable[[Any], int] | None = None,
    ) -> "CompactedContext":
        """Return an LLM-enriched context while preserving the safety envelope."""

        candidate = replace(self, semantic_summary=summary, token_count=0, digest="")
        payload = candidate._payload(include_digest=False)
        counter = token_counter or estimate_tokens
        token_count = counter(payload)
        if token_count > self.budget_tokens:
            raise ContextInvariantError("semantic compaction summary exceeds the context budget")
        enriched = replace(candidate, token_count=token_count, digest=_digest(payload))
        validate_context(enriched)
        return enriched

    def to_manifest(self) -> dict[str, Any]:
        """Return bounded checkpoint metadata without copying the prompt."""

        coverage = self._evidence_coverage()
        return {
            "schema_version": self.schema_version,
            "compaction_version": self.compaction_version,
            "digest": self.digest,
            "token_count": self.token_count,
            "budget_tokens": self.budget_tokens,
            "selected_evidence_ids": list(self.citable_evidence_ids),
            **coverage,
            "required_evidence_ids": list(self.required_evidence_ids),
            "omitted_evidence_count": self.omitted_evidence_count,
            "omitted_evidence_sample": list(self.omitted_evidence_sample),
            "omitted_memory_count": self.omitted_memory_count,
            "omitted_memory_sample": list(self.omitted_memory_sample),
        }


class ContextCompactor:
    """Build and validate a deterministic, bounded investigation context."""

    def __init__(
        self,
        *,
        max_tokens: int = 8000,
        reserved_tokens: int = 0,
        token_counter: Callable[[Any], int] | None = None,
    ) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if reserved_tokens < 0 or reserved_tokens >= max_tokens:
            raise ValueError("reserved_tokens must be non-negative and less than max_tokens")
        self.max_tokens = max_tokens
        self.selection_budget = max_tokens - reserved_tokens
        self.token_counter = token_counter or estimate_tokens

    def compact(
        self,
        *,
        base_report: Mapping[str, Any] | Any,
        evidence: Iterable[Mapping[str, Any]],
        memories: Iterable[Mapping[str, Any]],
        tenant_id: str,
        workflow_context: Mapping[str, Any] | None = None,
    ) -> CompactedContext:
        report = base_report.to_dict() if hasattr(base_report, "to_dict") else dict(base_report)
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ContextInvariantError("tenant_id is required for context compaction")

        evidence_candidates = self._normalize_evidence(evidence, tenant)
        memory_candidates = self._normalize_memories(memories, tenant)
        required_ids = self._required_evidence_ids(report)
        claims = self._claims(report)
        workflow = self._workflow_context(workflow_context, tenant)
        invariants = {
            **DEFAULT_INVARIANTS,
            "tenant_id": tenant,
        }

        evidence_by_id = {candidate.evidence_id: candidate for candidate in evidence_candidates}
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()

        # Required citations are admitted as references first. We then upgrade
        # as many of those references to full records as the budget allows.
        # This prevents an early full record from crowding out a later
        # mandatory citation and makes reference-only compaction deterministic.
        for evidence_id in required_ids:
            candidate = evidence_by_id.get(evidence_id)
            entry = candidate.preview(reference_only=True) if candidate else {
                "id": evidence_id,
                "kind": "evidence_reference",
                "data": {"reference_only": True},
            }
            if not self._fits(
                report=report,
                claims=claims,
                invariants=invariants,
                workflow=workflow,
                evidence=[*selected, entry],
                memories=[],
                unresolved_questions=report.get("unresolved_questions", []),
                failed_hypotheses=workflow.get("failed_hypotheses", []),
            ):
                raise ContextInvariantError(f"mandatory evidence reference {evidence_id} exceeds the context budget")
            selected.append(entry)
            selected_ids.add(evidence_id)

        for index, evidence_id in enumerate(required_ids):
            candidate = evidence_by_id.get(evidence_id)
            if candidate is None:
                continue
            entry = candidate.preview()
            proposed = [*selected]
            proposed[index] = entry
            if self._fits(
                report=report,
                claims=claims,
                invariants=invariants,
                workflow=workflow,
                evidence=proposed,
                memories=[],
                unresolved_questions=report.get("unresolved_questions", []),
                failed_hypotheses=workflow.get("failed_hypotheses", []),
            ):
                selected[index] = entry

        optional_evidence = [
            candidate
            for candidate in evidence_candidates
            if candidate.evidence_id not in selected_ids
        ]
        for candidate in sorted(optional_evidence, key=lambda item: (-item.priority, item.evidence_id)):
            entry = candidate.preview()
            if self._fits(
                report=report,
                claims=claims,
                invariants=invariants,
                workflow=workflow,
                evidence=[*selected, entry],
                memories=[],
                unresolved_questions=report.get("unresolved_questions", []),
                failed_hypotheses=workflow.get("failed_hypotheses", []),
            ):
                selected.append(entry)
                selected_ids.add(candidate.evidence_id)

        selected_memories: list[dict[str, Any]] = []
        for candidate in sorted(memory_candidates, key=lambda item: (-item.priority, item.evidence_id)):
            entry = {"id": candidate.evidence_id, "data": _bounded(candidate.data, string_limit=900, list_limit=10)}
            if self._fits(
                report=report,
                claims=claims,
                invariants=invariants,
                workflow=workflow,
                evidence=selected,
                memories=[*selected_memories, entry],
                unresolved_questions=report.get("unresolved_questions", []),
                failed_hypotheses=workflow.get("failed_hypotheses", []),
            ):
                selected_memories.append(entry)

        selected_memory_ids = {str(item["id"]) for item in selected_memories}
        all_evidence_ids = {candidate.evidence_id for candidate in evidence_candidates}
        all_memory_ids = {candidate.evidence_id for candidate in memory_candidates}
        omitted_evidence = sorted(all_evidence_ids - selected_ids)
        omitted_memories = sorted(all_memory_ids - selected_memory_ids)
        citable_ids = list(dict.fromkeys(str(item["id"]) for item in selected))

        context = self._build_context(
            report=report,
            claims=claims,
            invariants=invariants,
            workflow=workflow,
            evidence=selected,
            memories=selected_memories,
            citable_ids=citable_ids,
            required_ids=required_ids,
            omitted_evidence=omitted_evidence,
            omitted_memories=omitted_memories,
        )
        validate_context(context)
        return context

    def _normalize_evidence(
        self,
        records: Iterable[Mapping[str, Any]],
        tenant_id: str,
    ) -> list[_EvidenceCandidate]:
        candidates: dict[str, _EvidenceCandidate] = {}
        for raw in records:
            if not isinstance(raw, Mapping):
                continue
            evidence_id = _evidence_id(raw)
            if not evidence_id:
                continue
            data = raw.get("data") if isinstance(raw.get("data"), Mapping) else raw
            if not isinstance(data, Mapping):
                data = {"value": data}
            source_tenant = _tenant_from(data)
            if source_tenant and source_tenant not in {tenant_id, "public"}:
                raise ContextInvariantError(
                    f"evidence {evidence_id} belongs to tenant {source_tenant}, not {tenant_id}"
                )
            kind = str(raw.get("kind") or data.get("source_type") or "evidence")
            priority = self._priority(raw, kind)
            candidate = _EvidenceCandidate(evidence_id, kind, dict(data), priority)
            previous = candidates.get(evidence_id)
            if previous is None or candidate.priority > previous.priority:
                candidates[evidence_id] = candidate
        return list(candidates.values())

    def _normalize_memories(
        self,
        records: Iterable[Mapping[str, Any]],
        tenant_id: str,
    ) -> list[_EvidenceCandidate]:
        candidates: dict[str, _EvidenceCandidate] = {}
        for raw in records:
            if not isinstance(raw, Mapping):
                continue
            source_tenant = _tenant_from(raw)
            if source_tenant and source_tenant != tenant_id:
                raise ContextInvariantError(
                    f"memory record belongs to tenant {source_tenant}, not {tenant_id}"
                )
            memory_id = _memory_id(raw)
            candidate = _EvidenceCandidate(memory_id, "memory", dict(raw), _numeric_score(raw))
            previous = candidates.get(memory_id)
            if previous is None or candidate.priority > previous.priority:
                candidates[memory_id] = candidate
        return list(candidates.values())

    @staticmethod
    def _priority(item: Mapping[str, Any], kind: str) -> float:
        base = {
            "incident": 500.0,
            "invoice": 450.0,
            "application_log": 400.0,
            "deployment": 350.0,
            "vector_evidence": 300.0,
            "evidence": 250.0,
        }.get(kind, 200.0)
        data = item.get("data") if isinstance(item.get("data"), Mapping) else item
        metadata = data.get("metadata") if isinstance(data, Mapping) and isinstance(data.get("metadata"), Mapping) else {}
        if str(metadata.get("source_type") or data.get("source_type") or "").lower() == "runbook":
            base += 50.0
        return base + _numeric_score(item)

    @staticmethod
    def _required_evidence_ids(report: Mapping[str, Any]) -> list[str]:
        identifiers: list[str] = []
        identifiers.extend(str(value) for value in report.get("evidence_ids", []) if value)
        for claim in report.get("evidence_claims", []):
            if not isinstance(claim, Mapping):
                continue
            identifiers.extend(str(value) for value in claim.get("evidence_ids", []) if value)
        return list(dict.fromkeys(identifiers))

    @staticmethod
    def _claims(report: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
        claims: dict[str, list[dict[str, Any]]] = {"facts": [], "relationships": [], "hypotheses": []}
        names = {"fact": "facts", "relationship": "relationships", "hypothesis": "hypotheses"}
        for claim in report.get("evidence_claims", []):
            if not isinstance(claim, Mapping):
                continue
            bucket = names.get(str(claim.get("type") or "").lower())
            if bucket:
                claims[bucket].append(_bounded(dict(claim), string_limit=900, list_limit=10))
        return claims

    @staticmethod
    def _workflow_context(
        value: Mapping[str, Any] | None,
        tenant_id: str,
    ) -> dict[str, Any]:
        source = value if isinstance(value, Mapping) else {}
        allowed = (
            "workflow_id",
            "current_step",
            "completed_steps",
            "checkpoint_version",
            "status",
            "request",
            "plan",
            "failed_hypotheses",
        )
        result = {key: _bounded(source[key], string_limit=800, list_limit=20) for key in allowed if key in source}
        result["tenant_id"] = tenant_id
        if not isinstance(result.get("completed_steps"), list):
            result["completed_steps"] = []
        if not isinstance(result.get("failed_hypotheses"), list):
            result["failed_hypotheses"] = []
        return result

    def _fits(
        self,
        *,
        report: Mapping[str, Any],
        claims: dict[str, list[dict[str, Any]]],
        invariants: dict[str, Any],
        workflow: dict[str, Any],
        evidence: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        unresolved_questions: Any,
        failed_hypotheses: Any,
    ) -> bool:
        payload = self._payload_without_metadata(
            report=report,
            claims=claims,
            invariants=invariants,
            workflow=workflow,
            evidence=evidence,
            memories=memories,
            citable_ids=[str(item["id"]) for item in evidence],
            required_ids=self._required_evidence_ids(report),
            omitted_evidence_count=0,
            omitted_evidence_sample=[],
            omitted_memory_count=0,
            omitted_memory_sample=[],
            unresolved_questions=unresolved_questions,
            failed_hypotheses=failed_hypotheses,
        )
        return self.token_counter(payload) <= self.selection_budget

    def _build_context(
        self,
        *,
        report: Mapping[str, Any],
        claims: dict[str, list[dict[str, Any]]],
        invariants: dict[str, Any],
        workflow: dict[str, Any],
        evidence: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        citable_ids: list[str],
        required_ids: list[str],
        omitted_evidence: list[str],
        omitted_memories: list[str],
    ) -> CompactedContext:
        unresolved_questions = [str(value) for value in report.get("unresolved_questions", []) if str(value).strip()]
        failed_hypotheses = [str(value) for value in workflow.get("failed_hypotheses", []) if str(value).strip()]
        baseline_report = _bounded(dict(report), string_limit=1200, list_limit=20)
        context_without_digest = self._payload_without_metadata(
            report=baseline_report,
            claims=claims,
            invariants=invariants,
            workflow=workflow,
            evidence=evidence,
            memories=memories,
            citable_ids=citable_ids,
            required_ids=required_ids,
            omitted_evidence_count=len(omitted_evidence),
            omitted_evidence_sample=omitted_evidence[:25],
            omitted_memory_count=len(omitted_memories),
            omitted_memory_sample=omitted_memories[:25],
            unresolved_questions=unresolved_questions,
            failed_hypotheses=failed_hypotheses,
        )
        token_count = self.token_counter(context_without_digest)
        digest = _digest(context_without_digest)
        return CompactedContext(
            schema_version=CONTEXT_SCHEMA_VERSION,
            compaction_version=COMPACTION_VERSION,
            invariants=invariants,
            workflow=workflow,
            baseline_report=baseline_report,
            facts=claims["facts"],
            relationships=claims["relationships"],
            hypotheses=claims["hypotheses"],
            semantic_summary={},
            unresolved_questions=unresolved_questions,
            failed_hypotheses=failed_hypotheses,
            evidence=evidence,
            memories=memories,
            citable_evidence_ids=citable_ids,
            required_evidence_ids=required_ids,
            omitted_evidence_count=len(omitted_evidence),
            omitted_evidence_sample=omitted_evidence[:25],
            omitted_memory_count=len(omitted_memories),
            omitted_memory_sample=omitted_memories[:25],
            token_count=token_count,
            budget_tokens=self.max_tokens,
            digest=digest,
        )

    @staticmethod
    def _payload_without_metadata(
        *,
        report: Mapping[str, Any],
        claims: dict[str, list[dict[str, Any]]],
        invariants: dict[str, Any],
        workflow: dict[str, Any],
        evidence: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        citable_ids: list[str],
        required_ids: list[str],
        omitted_evidence_count: int,
        omitted_evidence_sample: list[str],
        omitted_memory_count: int,
        omitted_memory_sample: list[str],
        unresolved_questions: Any,
        failed_hypotheses: Any,
        semantic_summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected_ids = [str(item.get("id") or "") for item in evidence]
        full_ids = [
            str(item.get("id") or "")
            for item in evidence
            if not (isinstance(item.get("data"), Mapping) and item["data"].get("reference_only") is True)
        ]
        reference_only_ids = [item_id for item_id in selected_ids if item_id not in set(full_ids)]
        return {
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "compaction": {
                "version": COMPACTION_VERSION,
                "token_count": 0,
                "budget_tokens": 0,
                "digest": "",
            },
            "invariants": invariants,
            "workflow": workflow,
            "baseline_report": _bounded(dict(report), string_limit=1200, list_limit=20),
            "claims": claims,
            "semantic_summary": _bounded(dict(semantic_summary or {}), string_limit=1200, list_limit=20),
            "unresolved_questions": _bounded(unresolved_questions, string_limit=1200, list_limit=20),
            "failed_hypotheses": _bounded(failed_hypotheses, string_limit=1200, list_limit=20),
            "evidence": evidence,
            "memories": memories,
            "citable_evidence_ids": citable_ids,
            "evidence_coverage": {
                "required_evidence_ids": required_ids,
                "input_evidence_count": len(selected_ids) + omitted_evidence_count,
                "selected_evidence_count": len(selected_ids),
                "full_evidence_count": len(full_ids),
                "reference_only_evidence_count": len(reference_only_ids),
                "reference_only_evidence_sample": reference_only_ids[:25],
                "omitted_evidence_count": omitted_evidence_count,
                "omitted_evidence_sample": omitted_evidence_sample,
                "omitted_memory_count": omitted_memory_count,
                "omitted_memory_sample": omitted_memory_sample,
            },
        }


def validate_context(context: CompactedContext) -> None:
    """Validate safety and citation invariants before model invocation."""

    if context.schema_version != CONTEXT_SCHEMA_VERSION:
        raise ContextInvariantError("unsupported context schema version")
    if context.compaction_version != COMPACTION_VERSION:
        raise ContextInvariantError("unsupported compaction version")
    if context.invariants.get("read_only") is not True:
        raise ContextInvariantError("read-only invariant was not preserved")
    if context.invariants.get("tenant_id") != context.workflow.get("tenant_id"):
        raise ContextInvariantError("tenant invariant does not match workflow context")
    if context.token_count > context.budget_tokens:
        raise ContextInvariantError("compacted context exceeds its token budget")

    citable = set(context.citable_evidence_ids)
    selected_ids = [str(item.get("id") or "") for item in context.evidence]
    if len(selected_ids) != len(set(selected_ids)):
        raise ContextInvariantError("duplicate evidence IDs in compacted context")
    if set(selected_ids) != citable:
        raise ContextInvariantError("citable evidence IDs do not match selected evidence")
    if not set(context.required_evidence_ids).issubset(citable):
        missing = sorted(set(context.required_evidence_ids) - citable)
        raise ContextInvariantError(f"required evidence references were lost: {missing}")

    for claim in [*context.facts, *context.relationships, *context.hypotheses]:
        claim_type = str(claim.get("type") or "").lower()
        if claim_type not in {"fact", "relationship", "hypothesis"}:
            raise ContextInvariantError("unknown typed claim in compacted context")
        claim_ids = [str(value) for value in claim.get("evidence_ids", []) if value]
        if not claim_ids or not set(claim_ids).issubset(citable):
            raise ContextInvariantError("claim cites evidence outside the compacted context")

    semantic = context.semantic_summary
    if not isinstance(semantic, Mapping):
        raise ContextInvariantError("semantic summary must be an object")
    summary_ids = [str(value) for value in semantic.get("summary_evidence_ids", []) if value]
    if not set(summary_ids).issubset(citable):
        raise ContextInvariantError("semantic summary cites evidence outside the compacted context")
    for key in ("observations", "relationships", "hypotheses"):
        values = semantic.get(key, [])
        if not isinstance(values, list):
            raise ContextInvariantError(f"semantic summary {key} must be a list")
        for item in values:
            if not isinstance(item, Mapping) or not str(item.get("statement") or "").strip():
                raise ContextInvariantError("semantic summary contains an invalid statement")
            item_ids = [str(value) for value in item.get("evidence_ids", []) if value]
            if not item_ids or not set(item_ids).issubset(citable):
                raise ContextInvariantError("semantic summary statement cites unavailable evidence")


class ContextSummarizationError(RuntimeError):
    """Raised when the optional Bedrock semantic compaction pass is unusable."""


class BedrockContextSummarizer:
    """Ask Bedrock for semantic compression inside a deterministic envelope."""

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
        prompt_key: str = "context_compaction",
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
            raise ContextSummarizationError("boto3 is required for Bedrock context summarization") from exc
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )

    @staticmethod
    def _content_text(response: Mapping[str, Any]) -> str:
        content = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, Mapping) and item.get("text")
        )
        if not text:
            raise ContextSummarizationError("Bedrock response did not contain compaction text")
        return text

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
                raise ContextSummarizationError("Bedrock compaction response did not contain JSON")
            try:
                value = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError as exc:
                raise ContextSummarizationError("Bedrock compaction response contained invalid JSON") from exc
        if not isinstance(value, dict):
            raise ContextSummarizationError("Bedrock compaction response must be an object")
        return value

    @staticmethod
    def _claims(value: Any, *, label: str, citable: set[str]) -> list[dict[str, Any]]:
        if value in (None, []):
            return []
        if not isinstance(value, list):
            raise ContextSummarizationError(f"semantic compaction field {label} must be a list")
        result: list[dict[str, Any]] = []
        for item in value[:12]:
            if not isinstance(item, Mapping):
                raise ContextSummarizationError(f"semantic compaction field {label} contains a non-object")
            statement = str(item.get("statement") or "").strip()
            ids = list(dict.fromkeys(str(identifier) for identifier in item.get("evidence_ids", []) if identifier))
            if not statement or not ids or not set(ids).issubset(citable):
                raise ContextSummarizationError(f"semantic compaction {label} contains an unsupported citation")
            result.append({"statement": statement[:1200], "evidence_ids": ids})
        return result

    @classmethod
    def _normalize(cls, value: dict[str, Any], context: CompactedContext) -> dict[str, Any]:
        citable = set(context.citable_evidence_ids)
        summary = value.get("summary")
        if not isinstance(summary, str):
            raise ContextSummarizationError("semantic compaction summary must be text")
        raw_summary_ids = value.get("summary_evidence_ids", [])
        if not isinstance(raw_summary_ids, list):
            raise ContextSummarizationError("semantic compaction summary_evidence_ids must be a list")
        summary_ids = list(dict.fromkeys(str(identifier) for identifier in raw_summary_ids if identifier))
        if summary.strip() and (not summary_ids or not set(summary_ids).issubset(citable)):
            raise ContextSummarizationError("semantic compaction summary contains an unsupported citation")
        questions = value.get("unresolved_questions", [])
        if not isinstance(questions, list):
            raise ContextSummarizationError("semantic compaction unresolved_questions must be a list")
        return {
            "summary": summary.strip()[:2000],
            "summary_evidence_ids": summary_ids,
            "observations": cls._claims(value.get("observations"), label="observations", citable=citable),
            "relationships": cls._claims(value.get("relationships"), label="relationships", citable=citable),
            "hypotheses": cls._claims(value.get("hypotheses"), label="hypotheses", citable=citable),
            "unresolved_questions": [str(question).strip()[:800] for question in questions[:12] if str(question).strip()],
        }

    def summarize(self, context: CompactedContext) -> CompactedContext:
        """Add model-proposed semantic summaries without changing authority."""

        try:
            prompt_version_arn = self.prompt_resolver.resolve()
        except Exception as exc:
            raise ContextSummarizationError(f"context compaction prompt could not be resolved: {exc}") from exc
        manifest = context.to_manifest()
        user = json.dumps(
            {
                "context": context.to_prompt_dict(),
                "output_schema": {
                    "summary": "short semantic summary",
                    "summary_evidence_ids": ["copied citable evidence IDs"],
                    "observations": [{"statement": "short observation", "evidence_ids": ["ID"]}],
                    "relationships": [{"statement": "short relationship", "evidence_ids": ["ID"]}],
                    "hypotheses": [{"statement": "short hypothesis", "evidence_ids": ["ID"]}],
                    "unresolved_questions": ["question"],
                },
            },
            sort_keys=True,
        )
        self.logger(
            {
                "event": "llm_compaction_started",
                "model_id": self.model_id,
                "prompt_version_arn": prompt_version_arn,
                "context_digest": context.digest,
                "input_token_count": context.token_count,
                "input_evidence_count": manifest["input_evidence_count"],
                "selected_evidence_count": manifest["selected_evidence_count"],
                "full_evidence_count": manifest["full_evidence_count"],
                "reference_only_evidence_count": manifest["reference_only_evidence_count"],
                "omitted_evidence_count": manifest["omitted_evidence_count"],
                "prompt_variables_preview": user[:6000],
            }
        )
        try:
            response = self.client.converse(
                modelId=prompt_version_arn,
                promptVariables={"input": {"text": user}},
            )
            candidate = self._parse(self._content_text(response))
            summary = self._normalize(candidate, context)
            enriched = context.with_semantic_summary(summary)
        except ContextSummarizationError:
            raise
        except Exception as exc:
            raise ContextSummarizationError(str(exc)) from exc
        self.logger(
            {
                "event": "llm_compaction_completed",
                "model_id": self.model_id,
                "prompt_version_arn": prompt_version_arn,
                "context_digest": enriched.digest,
                "input_token_count": enriched.token_count,
                "input_evidence_count": manifest["input_evidence_count"],
                "selected_evidence_count": manifest["selected_evidence_count"],
                "full_evidence_count": manifest["full_evidence_count"],
                "reference_only_evidence_count": manifest["reference_only_evidence_count"],
                "omitted_evidence_count": manifest["omitted_evidence_count"],
                "stop_reason": response.get("stopReason"),
            }
        )
        return enriched
