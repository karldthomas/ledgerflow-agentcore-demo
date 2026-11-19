"""Context compaction, invariants, token budgeting, and prefix accounting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import Evidence, InvariantSet, MemoryRecord


def estimate_tokens(text: str) -> int:
    """Use a stable approximation when a model tokenizer is unavailable."""

    return max(1, len(text.split())) if text.strip() else 0


@dataclass(frozen=True)
class BudgetAllocation:
    total: int
    system: int
    task: int
    evidence: int
    memory: int
    history: int
    output_reserve: int


class TokenBudgetAllocator:
    def allocate(self, total: int, *, output_reserve: int = 512) -> BudgetAllocation:
        if total <= output_reserve:
            raise ValueError("total token budget must exceed output reserve")
        usable = total - output_reserve
        return BudgetAllocation(
            total=total,
            system=max(64, int(usable * 0.16)),
            task=max(64, int(usable * 0.14)),
            evidence=max(128, int(usable * 0.40)),
            memory=max(64, int(usable * 0.12)),
            history=max(64, usable - int(usable * 0.16) - int(usable * 0.14) - int(usable * 0.40) - int(usable * 0.12)),
            output_reserve=output_reserve,
        )


class PrefixCache:
    """Small local accounting model for provider prompt-prefix caching."""

    def __init__(self) -> None:
        self._prefixes: set[str] = set()
        self.hits = 0
        self.misses = 0

    def lookup(self, prefix: str) -> bool:
        key = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
        if key in self._prefixes:
            self.hits += 1
            return True
        self._prefixes.add(key)
        self.misses += 1
        return False

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


@dataclass
class CompactedContext:
    text: str
    token_count: int
    preserved_invariants: InvariantSet
    preserved_evidence_ids: list[str]
    preserved_memory_ids: list[str]
    dropped_items: list[str] = field(default_factory=list)
    compaction_count: int = 0
    prefix_cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "token_count": self.token_count,
            "preserved_invariants": self.preserved_invariants.to_dict(),
            "preserved_evidence_ids": self.preserved_evidence_ids,
            "preserved_memory_ids": self.preserved_memory_ids,
            "dropped_items": self.dropped_items,
            "compaction_count": self.compaction_count,
            "prefix_cache_hit": self.prefix_cache_hit,
        }


class ContextCompactor:
    """Build bounded context while retaining safety and citation anchors."""

    def __init__(self, allocator: TokenBudgetAllocator | None = None, prefix_cache: PrefixCache | None = None) -> None:
        self.allocator = allocator or TokenBudgetAllocator()
        self.prefix_cache = prefix_cache or PrefixCache()

    @staticmethod
    def _clip(text: str, budget: int) -> str:
        words = text.split()
        return " ".join(words[: max(0, budget)])

    @staticmethod
    def _invariant_text(invariants: InvariantSet) -> str:
        return "\n".join(
            [
                "INVARIANTS:",
                f"- no_financial_writes={invariants.no_financial_writes}",
                f"- evidence_required={invariants.require_evidence_for_findings}",
                f"- approval_required_for_remediation={invariants.require_approval_for_remediation}",
            ]
        )

    def compact(
        self,
        *,
        task: str,
        workflow_state: dict[str, Any],
        evidence: Iterable[Evidence],
        memories: Iterable[MemoryRecord],
        history: Iterable[str] = (),
        total_budget: int = 1800,
        output_reserve: int = 400,
        invariants: InvariantSet | None = None,
        previous_compactions: int = 0,
    ) -> CompactedContext:
        invariants = invariants or InvariantSet()
        allocation = self.allocator.allocate(total_budget, output_reserve=output_reserve)
        evidence_list = sorted(list(evidence), key=lambda item: (-item.score, item.evidence_id))
        memory_list = list(memories)
        history_list = list(history)
        invariant_text = self._invariant_text(invariants)
        stable_prefix = invariant_text + "\nSYSTEM: LedgerFlow evidence-grounded investigation agent."
        cache_hit = self.prefix_cache.lookup(stable_prefix)

        evidence_texts: list[str] = []
        preserved_evidence_ids: list[str] = []
        evidence_budget = allocation.evidence
        for item in evidence_list:
            line = f"[{item.evidence_id}] {item.title}: {item.content}"
            line_tokens = estimate_tokens(line)
            if evidence_texts and estimate_tokens("\n".join(evidence_texts)) + line_tokens > evidence_budget:
                continue
            evidence_texts.append(line)
            preserved_evidence_ids.append(item.evidence_id)

        memory_texts: list[str] = []
        preserved_memory_ids: list[str] = []
        for item in memory_list:
            line = f"[{item.memory_id}] {item.subject_id}/{item.key}: {item.value} (source={item.source_id})"
            if memory_texts and estimate_tokens("\n".join(memory_texts)) + estimate_tokens(line) > allocation.memory:
                continue
            memory_texts.append(line)
            preserved_memory_ids.append(item.memory_id)

        history_text = self._clip("\n".join(history_list), allocation.history)
        state_text = json.dumps(workflow_state, sort_keys=True)
        task_text = self._clip(task, allocation.task)
        text = (
            f"{stable_prefix}\n"
            f"TASK: {task_text}\n"
            f"WORKFLOW_STATE: {state_text}\n"
            f"{invariant_text}\n"
            f"MEMORY:\n{chr(10).join(memory_texts)}\n"
            f"EVIDENCE:\n{chr(10).join(evidence_texts)}\n"
            f"HISTORY:\n{history_text}"
        )

        # If the structured sections still exceed the budget, compact only the
        # least authoritative free text. Invariant and evidence IDs remain.
        dropped: list[str] = []
        if estimate_tokens(text) + output_reserve > total_budget:
            if history_text:
                dropped.append("history")
                history_text = "[history compacted]"
            text = (
                f"{stable_prefix}\nTASK: {task_text}\nWORKFLOW_STATE: {state_text}\n{invariant_text}\n"
                f"MEMORY:\n{chr(10).join(memory_texts)}\nEVIDENCE:\n{chr(10).join(evidence_texts)}\nHISTORY:\n{history_text}"
            )
        if estimate_tokens(text) + output_reserve > total_budget:
            # Preserve citation anchors in a final compact evidence index.
            dropped.extend(f"evidence:{item.evidence_id}" for item in evidence_list if item.evidence_id not in preserved_evidence_ids)
            evidence_index = " ".join(f"[{item.evidence_id}]" for item in evidence_list)
            text = f"{stable_prefix}\nTASK: {task_text}\n{invariant_text}\nEVIDENCE_INDEX: {evidence_index}"

        result = CompactedContext(
            text=text,
            token_count=estimate_tokens(text),
            preserved_invariants=invariants,
            preserved_evidence_ids=preserved_evidence_ids,
            preserved_memory_ids=preserved_memory_ids,
            dropped_items=dropped,
            compaction_count=previous_compactions + (1 if dropped else 0),
            prefix_cache_hit=cache_hit,
        )
        self.validate_invariants(result)
        return result

    @staticmethod
    def validate_invariants(context: CompactedContext) -> None:
        for phrase in ("INVARIANTS:", "no_financial_writes=True", "evidence_required=True", "approval_required_for_remediation=True"):
            if phrase not in context.text:
                raise AssertionError(f"compaction dropped required invariant: {phrase}")
        if context.preserved_invariants.require_evidence_for_findings and not context.preserved_evidence_ids and "EVIDENCE_INDEX:" not in context.text:
            raise AssertionError("compaction dropped all evidence anchors")
