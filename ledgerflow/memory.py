"""Verified-facts memory lifecycle for the local POC."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .db import Database, utc_now
from .models import MemoryRecord, parse_iso


class MemoryService:
    """Manage memory records with deduplication, supersession and expiry."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _now(now: datetime | None = None) -> datetime:
        return now or utc_now()

    @staticmethod
    def _record_from_row(row: Any) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"],
            tenant_id=row["tenant_id"],
            subject_id=row["subject_id"],
            key=row["key"],
            value=row["value"],
            source_id=row["source_id"],
            confidence=float(row["confidence"]),
            observed_at=row["observed_at"],
            expires_at=row["expires_at"],
            status=row["status"],
            provenance=json.loads(row["provenance_json"]),
            authority=int(row["authority"]),
        )

    def _expire(self, now: datetime) -> int:
        changed = self.db.execute(
            """UPDATE memory_records SET status = 'expired'
               WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?""",
            (now.isoformat(),),
        ).rowcount
        if changed:
            self.db.connection.commit()
            self.db.log_event("memory_expired", {"count": changed, "at": now.isoformat()})
        return changed

    def active(
        self,
        tenant_id: str,
        subject_id: str | None = None,
        key: str | None = None,
        *,
        now: datetime | None = None,
    ) -> list[MemoryRecord]:
        effective_now = self._now(now)
        self._expire(effective_now)
        clauses = ["tenant_id = ?", "status = 'active'", "(expires_at IS NULL OR expires_at > ?)"]
        params: list[Any] = [tenant_id, effective_now.isoformat()]
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if key is not None:
            clauses.append("key = ?")
            params.append(key)
        rows = self.db.query_all(
            f"SELECT * FROM memory_records WHERE {' AND '.join(clauses)} ORDER BY authority DESC, confidence DESC, observed_at DESC",
            tuple(params),
        )
        return [self._record_from_row(row) for row in rows]

    def relevant(self, tenant_id: str, query: str, *, subject_id: str | None = None, limit: int = 10) -> list[MemoryRecord]:
        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        candidates = self.active(tenant_id, subject_id)
        scored: list[tuple[float, MemoryRecord]] = []
        for record in candidates:
            searchable = f"{record.subject_id} {record.key} {record.value} {' '.join(record.provenance)}".lower()
            overlap = len(terms & set(re.findall(r"[a-z0-9]+", searchable)))
            score = overlap * 10 + record.authority + record.confidence
            if overlap:
                scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], -item[1].authority, item[1].memory_id))
        return [record for _, record in scored[: max(1, limit)]]

    def observe(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        key: str,
        value: str,
        source_id: str,
        confidence: float,
        observed_at: str | None = None,
        expires_at: str | None = None,
        authority: int = 1,
        provenance: list[str] | None = None,
    ) -> MemoryRecord:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        timestamp = observed_at or utc_now().isoformat()
        existing = self.db.query_one(
            """SELECT * FROM memory_records
               WHERE tenant_id = ? AND subject_id = ? AND key = ? AND value = ? AND status = 'active'
               ORDER BY observed_at DESC LIMIT 1""",
            (tenant_id, subject_id, key, value),
        )
        if existing:
            combined = sorted(set(json.loads(existing["provenance_json"]) + (provenance or []) + [source_id]))
            self.db.execute(
                "UPDATE memory_records SET provenance_json = ?, confidence = MAX(confidence, ?), observed_at = ? WHERE memory_id = ?",
                (json.dumps(combined), confidence, timestamp, existing["memory_id"]),
            )
            self.db.connection.commit()
            self.db.log_event("memory_deduplicated", {"memory_id": existing["memory_id"], "source_id": source_id})
            row = self.db.query_one("SELECT * FROM memory_records WHERE memory_id = ?", (existing["memory_id"],))
            assert row is not None
            return self._record_from_row(row)

        conflicts = self.db.query_all(
            """SELECT memory_id FROM memory_records
               WHERE tenant_id = ? AND subject_id = ? AND key = ? AND status = 'active' AND value <> ?""",
            (tenant_id, subject_id, key, value),
        )
        if conflicts:
            self.db.execute(
                """UPDATE memory_records SET status = 'superseded'
                   WHERE tenant_id = ? AND subject_id = ? AND key = ? AND status = 'active' AND value <> ?""",
                (tenant_id, subject_id, key, value),
            )
        memory_id = f"MEM-LOCAL-{uuid4().hex[:12]}"
        self.db.execute(
            """INSERT INTO memory_records(
                memory_id, tenant_id, subject_id, key, value, source_id, confidence,
                observed_at, expires_at, status, provenance_json, authority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                memory_id,
                tenant_id,
                subject_id,
                key,
                value,
                source_id,
                confidence,
                timestamp,
                expires_at,
                json.dumps(sorted(set((provenance or []) + [source_id]))),
                authority,
            ),
        )
        self.db.connection.commit()
        self.db.log_event(
            "memory_observed",
            {"memory_id": memory_id, "superseded_count": len(conflicts), "tenant_id": tenant_id, "key": key},
        )
        row = self.db.query_one("SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,))
        assert row is not None
        return self._record_from_row(row)

    def consolidate(self, tenant_id: str, subject_id: str, key: str) -> list[MemoryRecord]:
        """Return the currently authoritative records for one memory key."""

        records = self.active(tenant_id, subject_id, key)
        if not records:
            return []
        authoritative = records[0]
        for record in records[1:]:
            if record.value != authoritative.value:
                self.db.execute("UPDATE memory_records SET status = 'superseded' WHERE memory_id = ?", (record.memory_id,))
        self.db.connection.commit()
        return self.active(tenant_id, subject_id, key)
