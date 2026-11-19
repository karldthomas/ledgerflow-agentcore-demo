"""CloudFormation custom resource for S3 Vectors enhanced hybrid search."""

from __future__ import annotations

import json
import urllib.request

import boto3


client = boto3.client("s3vectors")


def _respond(event: dict, status: str, data: dict, reason: str | None = None) -> None:
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or "See CloudWatch Logs",
            "PhysicalResourceId": event.get("PhysicalResourceId", event["ResourceProperties"]["IndexArn"]),
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": data,
        }
    ).encode("utf-8")
    request = urllib.request.Request(event["ResponseURL"], data=body, method="PUT")
    request.add_header("content-type", "")
    request.add_header("content-length", str(len(body)))
    urllib.request.urlopen(request).read()


def handler(event: dict, context: object) -> None:
    props = event["ResourceProperties"]
    try:
        if event["RequestType"] != "Delete":
            vector_bucket_name = props["VectorBucketName"]
            index_name = props["IndexName"]
            client.put_vector_bucket_default_index_mode(
                vectorBucketName=vector_bucket_name,
                defaultIndexMode="ENHANCED",
            )
            index = client.get_index(vectorBucketName=vector_bucket_name, indexName=index_name)["index"]
            if index.get("indexMode") != "ENHANCED":
                client.update_index_mode(
                    vectorBucketName=vector_bucket_name,
                    indexName=index_name,
                    indexMode="ENHANCED",
                )

            requested_fields = [
                {"metadataKey": value.strip(), "language": "en"}
                for value in props["TextSearchFields"].split(",")
                if value.strip()
            ]
            index = client.get_index(vectorBucketName=vector_bucket_name, indexName=index_name)["index"]
            existing_fields = {
                field.get("metadataKey")
                for field in index.get("metadataConfiguration", {}).get("textSearchConfigurations", [])
            }
            fields_to_add = [field for field in requested_fields if field["metadataKey"] not in existing_fields]
            if fields_to_add:
                client.update_metadata_configuration(
                    vectorBucketName=vector_bucket_name,
                    indexName=index_name,
                    add={"textSearchConfigurations": fields_to_add},
                )

            index = client.get_index(vectorBucketName=vector_bucket_name, indexName=index_name)["index"]
            configured = {
                field.get("metadataKey"): field.get("status")
                for field in index.get("metadataConfiguration", {}).get("textSearchConfigurations", [])
            }

        _respond(
            event,
            "SUCCESS",
            {
                "IndexMode": "ENHANCED",
                "TextSearchFields": props["TextSearchFields"],
                "TextSearchStatuses": json.dumps(configured if event["RequestType"] != "Delete" else {}),
            },
        )
    except Exception as exc:  # pragma: no cover - exercised by CloudFormation
        print(json.dumps({"error": str(exc), "event": event}))
        _respond(event, "FAILED", {}, str(exc))
