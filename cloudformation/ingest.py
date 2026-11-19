#!/usr/bin/env python3
"""Load the deterministic fixture set into DynamoDB, S3, and S3 Vectors.

Run this after stacks 01 and 02 exist. It uses Amazon Titan Text Embeddings V2
for the same dimension configured on the S3 Vectors index, and writes the
searchable ``text``/``title`` metadata fields used by BM25 hybrid queries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

# Support both ``python -m cloudformation.ingest`` and the documented direct
# invocation ``python cloudformation/ingest.py``.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ledgerflow.db import Database


def _ddb_item(row: Any, table: str) -> dict[str, Any]:
    mappings = {
        "customers": {"CustomerId": "customer_id", "TenantId": "tenant_id", "Name": "name", "Plan": "plan", "Active": "active"},
        "incidents": {
            "IncidentId": "incident_id", "CustomerId": "customer_id", "TenantId": "tenant_id", "Category": "category",
            "Severity": "severity", "Description": "description", "DeploymentVersion": "deployment_version", "Status": "status", "CreatedAt": "created_at",
        },
        "invoices": {
            "InvoiceId": "invoice_id", "CustomerId": "customer_id", "TenantId": "tenant_id", "SubscriptionId": "subscription_id",
            "RenewalId": "renewal_id", "AmountCents": "amount_cents", "Status": "status", "IssuedAt": "issued_at",
        },
        "application_logs": {
            "LogId": "log_id", "CustomerId": "customer_id", "TenantId": "tenant_id", "Timestamp": "timestamp", "Service": "service", "Event": "event", "PayloadJson": "payload_json",
        },
        "deployments": {
            "DeploymentId": "deployment_id", "TenantId": "tenant_id", "Version": "version", "DeployedAt": "deployed_at", "Service": "service", "ChangeSummary": "change_summary", "Status": "status",
        },
    }
    item: dict[str, Any] = {}
    for destination, source in mappings[table].items():
        value = row[source]
        if table == "customers" and destination == "Active":
            value = bool(value)
        if table == "application_logs" and destination == "PayloadJson":
            value = json.loads(value)
        item[destination] = value
    return item


def _document_vector(document: Any, embedding: list[float], tenant_id: str) -> dict[str, Any]:
    body = document["body"]
    key = f"{document['document_id']}#1"
    metadata = {
        "document_id": document["document_id"],
        "source_id": document["document_id"],
        "tenant_id": tenant_id,
        "source_type": document["doc_type"],
        "title": document["title"],
        "text": body,
        "version": document["version"],
        "document_version": document["version"],
        "authority": document["authority"],
        "observed_at": document["updated_at"],
        "effective_at": document["updated_at"],
        "status": "active",
        "source_uri": f"fixtures/documents/{document['document_id']}.json",
        "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    return {"key": key, "data": {"float32": [float(value) for value in embedding]}, "metadata": metadata}


def _embedding(client: Any, text: str, dimensions: int, model_id: str) -> list[float]:
    response = client.invoke_model(
        modelId=model_id,
        body=json.dumps({"inputText": text, "dimensions": dimensions, "normalize": True}).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]


def ingest(
    *,
    db_path: str | Path,
    context: dict[str, str],
    region: str,
    vector_dimension: int,
    embedding_model_id: str,
    upload: bool,
) -> dict[str, Any]:
    db = Database(db_path)
    db.seed()
    summary: dict[str, Any] = {"tables": {}, "documents": 0, "vectors": 0, "upload": upload}
    if not upload:
        summary["tables"] = db.counts()
        summary["documents"] = int(db.query_one("SELECT COUNT(*) AS n FROM documents")["n"])
        return summary

    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required for --upload") from exc
    dynamodb = boto3.resource("dynamodb", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    vectors = boto3.client("s3vectors", region_name=region)
    bedrock = boto3.client("bedrock-runtime", region_name=region)

    table_context = {
        "customers": "CF_OUT_CustomersTableName",
        "incidents": "CF_OUT_IncidentsTableName",
        "invoices": "CF_OUT_InvoicesTableName",
        "application_logs": "CF_OUT_ApplicationLogsTableName",
        "deployments": "CF_OUT_DeploymentsTableName",
    }
    for logical_table, context_key in table_context.items():
        table_name = context.get(context_key)
        if not table_name:
            raise ValueError(f"missing {context_key} in deployment context")
        table = dynamodb.Table(table_name)
        rows = db.query_all(f"SELECT * FROM {logical_table}")
        with table.batch_writer() as batch:
            for row in rows:
                batch.put_item(Item=_ddb_item(row, logical_table))
        summary["tables"][logical_table] = len(rows)

    raw_bucket = context.get("CF_OUT_RawDataBucketName")
    if raw_bucket:
        documents = [{key: row[key] for key in row.keys()} for row in db.query_all("SELECT * FROM documents")]
        s3.put_object(
            Bucket=raw_bucket,
            Key="fixtures/documents.json",
            Body=json.dumps(documents, default=str).encode("utf-8"),
            ContentType="application/json",
        )
    vector_bucket = context.get("CF_OUT_VectorBucketName")
    vector_index = context.get("CF_OUT_VectorIndexName")
    if not vector_bucket or not vector_index:
        raise ValueError("missing S3 Vectors output context")
    documents = db.query_all("SELECT * FROM documents")
    pending: list[dict[str, Any]] = []
    for document in documents:
        tenant_id = document["tenant_id"] or "public"
        pending.append(
            _document_vector(
                document,
                _embedding(bedrock, f"{document['title']}\n{document['body']}", vector_dimension, embedding_model_id),
                tenant_id,
            )
        )
        if len(pending) == 100:
            vectors.put_vectors(vectorBucketName=vector_bucket, indexName=vector_index, vectors=pending)
            summary["vectors"] += len(pending)
            pending = []
    if pending:
        vectors.put_vectors(vectorBucketName=vector_bucket, indexName=vector_index, vectors=pending)
        summary["vectors"] += len(pending)
    summary["documents"] = len(documents)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-db",
        default=":memory:",
        help="optional SQLite path for inspecting generated fixtures (default: in-memory)",
    )
    parser.add_argument("--context-file", type=Path, default=Path("cloudformation/build/deployment-context-dev.json"))
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--vector-dimension", type=int, default=1024)
    parser.add_argument("--embedding-model-id", default=os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"))
    parser.add_argument("--upload", action="store_true")
    args = parser.parse_args(argv)
    context = json.loads(args.context_file.read_text(encoding="utf-8")) if args.context_file.exists() else {}
    print(
        json.dumps(
            ingest(
                db_path=args.fixture_db,
                context=context,
                region=args.region,
                vector_dimension=args.vector_dimension,
                embedding_model_id=args.embedding_model_id,
                upload=args.upload,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
