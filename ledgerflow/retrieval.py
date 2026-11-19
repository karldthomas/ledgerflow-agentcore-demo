"""Tenant-aware hybrid retrieval used by the local POC.

The production implementation maps this interface to S3 Vectors
``QueryVectors`` with an ``ENHANCED`` index.  The local implementation keeps
the same stages—metadata filtering, BM25-style lexical scoring, vector
similarity, native-like reciprocal-rank fusion, and reranking—without requiring
an SDK or an embedding service.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from .db import Database
from .models import Evidence, parse_iso


TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


def _hashed_vector(text: str, dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 else -1.0
        vector[index] += sign
    magnitude = math.sqrt(sum(value * value for value in vector))
    return [value / magnitude for value in vector] if magnitude else vector


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


@dataclass(frozen=True)
class RetrievalResult:
    evidence: Evidence
    keyword_score: float
    vector_score: float
    rrf_score: float
    rerank_score: float
    keyword_rank: int | None
    vector_rank: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.evidence.to_dict(),
            "keyword_score": self.keyword_score,
            "vector_score": self.vector_score,
            "rrf_score": self.rrf_score,
            "rerank_score": self.rerank_score,
            "keyword_rank": self.keyword_rank,
            "vector_rank": self.vector_rank,
        }


class HybridRetriever:
    """Deterministic stand-in for S3 Vectors hybrid QueryVectors."""

    def __init__(self, db: Database, vector_dimensions: int = 64, rrf_k: int = 60) -> None:
        self.db = db
        self.vector_dimensions = vector_dimensions
        self.rrf_k = rrf_k
        self.last_query: dict[str, Any] | None = None

    def _visible_documents(self, tenant_id: str, filters: dict[str, Any] | None) -> list[Any]:
        filters = filters or {}
        rows = self.db.query_all("SELECT * FROM documents ORDER BY document_id")
        visible = []
        for row in rows:
            # A NULL tenant is a public runbook. A private document is visible
            # only to the matching tenant.
            if row["tenant_id"] not in (None, tenant_id):
                continue
            if filters.get("doc_type") and row["doc_type"] != filters["doc_type"]:
                continue
            if filters.get("document_ids") and row["document_id"] not in set(filters["document_ids"]):
                continue
            if filters.get("min_authority") is not None and row["authority"] < int(filters["min_authority"]):
                continue
            if filters.get("version") and row["version"] != str(filters["version"]):
                continue
            visible.append(row)
        return visible

    @staticmethod
    def _document_text(row: Any) -> str:
        return " ".join(str(row[key]) for key in ("document_id", "doc_type", "title", "version", "body"))

    def _bm25_scores(self, query: str, rows: list[Any]) -> dict[str, float]:
        query_tokens = tokenize(query)
        if not query_tokens or not rows:
            return {row["document_id"]: 0.0 for row in rows}
        documents = {row["document_id"]: tokenize(self._document_text(row)) for row in rows}
        document_frequency = Counter(
            token for tokens in documents.values() for token in set(tokens) if token in set(query_tokens)
        )
        average_length = sum(len(tokens) for tokens in documents.values()) / max(1, len(documents))
        k1 = 1.2
        b = 0.75
        scores: dict[str, float] = {}
        total = len(documents)
        for document_id, tokens in documents.items():
            counts = Counter(tokens)
            length = len(tokens)
            score = 0.0
            for token in query_tokens:
                frequency = counts[token]
                if not frequency:
                    continue
                df = document_frequency[token]
                inverse_document_frequency = math.log(1 + (total - df + 0.5) / (df + 0.5))
                denominator = frequency + k1 * (1 - b + b * length / max(1, average_length))
                score += inverse_document_frequency * (frequency * (k1 + 1)) / denominator
            scores[document_id] = score
        return scores

    def _vector_scores(self, query: str, rows: list[Any]) -> dict[str, float]:
        query_vector = _hashed_vector(query, self.vector_dimensions)
        return {
            row["document_id"]: cosine(query_vector, _hashed_vector(self._document_text(row), self.vector_dimensions))
            for row in rows
        }

    def search(
        self,
        query: str,
        tenant_id: str,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 5,
        strategy: str = "hybrid",
    ) -> list[RetrievalResult]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        rows = self._visible_documents(tenant_id, filters)
        keyword_scores = self._bm25_scores(query, rows)
        vector_scores = self._vector_scores(query, rows)
        keyword_order = sorted(keyword_scores, key=lambda key: (-keyword_scores[key], key))
        vector_order = sorted(vector_scores, key=lambda key: (-vector_scores[key], key))
        keyword_rank = {document_id: index + 1 for index, document_id in enumerate(keyword_order) if keyword_scores[document_id] > 0}
        vector_rank = {document_id: index + 1 for index, document_id in enumerate(vector_order)}

        candidates: Iterable[str]
        if strategy == "keyword":
            candidates = keyword_order
        elif strategy == "vector":
            candidates = vector_order
        elif strategy == "hybrid":
            candidates = set(keyword_order) | set(vector_order)
        else:
            raise ValueError(f"unsupported retrieval strategy: {strategy}")

        rows_by_id = {row["document_id"]: row for row in rows}
        results: list[RetrievalResult] = []
        for document_id in candidates:
            row = rows_by_id[document_id]
            rrf_score = 0.0
            if strategy in ("keyword", "hybrid") and document_id in keyword_rank:
                rrf_score += 1.0 / (self.rrf_k + keyword_rank[document_id])
            if strategy in ("vector", "hybrid") and document_id in vector_rank:
                rrf_score += 1.0 / (self.rrf_k + vector_rank[document_id])
            freshness = 0.0
            observed_at = parse_iso(row["updated_at"])
            if observed_at:
                freshness = observed_at.timestamp() / 10_000_000_000
            rerank_score = (
                rrf_score * 1000.0
                + float(row["authority"]) * 0.05
                + freshness
                + (0.02 if row["tenant_id"] is None else 0.0)
            )
            evidence = Evidence(
                evidence_id=row["document_id"],
                source_type=row["doc_type"],
                title=row["title"],
                content=row["body"],
                tenant_id=row["tenant_id"],
                observed_at=row["updated_at"],
                metadata={
                    "version": row["version"],
                    "authority": row["authority"],
                    "tenant_id": row["tenant_id"],
                    "keyword_score": keyword_scores[document_id],
                    "vector_score": vector_scores[document_id],
                    "rrf_score": rrf_score,
                },
                score=rerank_score,
            )
            results.append(
                RetrievalResult(
                    evidence=evidence,
                    keyword_score=keyword_scores[document_id],
                    vector_score=vector_scores[document_id],
                    rrf_score=rrf_score,
                    rerank_score=rerank_score,
                    keyword_rank=keyword_rank.get(document_id),
                    vector_rank=vector_rank.get(document_id),
                )
            )
        results.sort(key=lambda result: (-result.rerank_score, result.evidence.evidence_id))
        self.last_query = {
            "query": query,
            "tenant_id": tenant_id,
            "filters": filters or {},
            "strategy": strategy,
            "top_k": top_k,
            "candidate_count": len(results),
        }
        self.db.log_event("hybrid_retrieval", self.last_query)
        return results[: max(1, top_k)]

    def search_evidence(self, query: str, tenant_id: str, **kwargs: Any) -> list[Evidence]:
        return [result.evidence for result in self.search(query, tenant_id, **kwargs)]

    def ingest_documents(self, documents: list[dict[str, Any]]) -> int:
        """Insert local documents; S3 Vectors ingestion is a separate adapter.

        This helper is intentionally idempotent and is useful for adding test
        fixtures without changing the production storage interface.
        """

        count = 0
        for document in documents:
            self.db.execute(
                """INSERT OR REPLACE INTO documents(
                    document_id, tenant_id, doc_type, title, version, updated_at,
                    authority, body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document["document_id"],
                    document.get("tenant_id"),
                    document.get("doc_type", "runbook"),
                    document["title"],
                    str(document.get("version", "1")),
                    document["updated_at"],
                    int(document.get("authority", 1)),
                    document["body"],
                ),
            )
            count += 1
        self.db.connection.commit()
        return count
