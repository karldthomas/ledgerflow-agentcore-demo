from __future__ import annotations

import importlib.util
import json
from pathlib import Path


class _DevToolsTable:
    def __init__(self):
        self.items: dict[str, dict[str, object]] = {}

    def query(self, **_kwargs):
        return {"Items": list(self.items.values())}

    def put_item(self, *, Item, **_kwargs):
        self.items[str(Item["ControlId"])] = dict(Item)

    def scan(self, **_kwargs):
        return {"Items": list(self.items.values())}

    def get_item(self, *, Key):
        item = self.items.get(str(Key["ControlId"]))
        return {"Item": dict(item)} if item else {}

    def update_item(self, *, Key, ExpressionAttributeValues, **_kwargs):
        item = self.items[str(Key["ControlId"])]
        item.update(
            {
                "Enabled": ExpressionAttributeValues[":false"],
                "DisabledAt": ExpressionAttributeValues[":now"],
                "DisabledBy": ExpressionAttributeValues[":actor"],
                "UpdatedAt": ExpressionAttributeValues[":now"],
            }
        )
        return {"Attributes": dict(item)}


class _DynamoResource:
    def __init__(self, table):
        self.table = table

    def Table(self, _name):
        return self.table


def _load_web_app(monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    spec = importlib.util.spec_from_file_location("ledgerflow_web_app_test", Path("lambda/web_app.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(path, method, claims, body=None):
    result = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
    }
    if body is not None:
        result["body"] = json.dumps(body)
    return result


def test_admin_dev_tools_can_arm_list_and_disarm(monkeypatch):
    module = _load_web_app(monkeypatch)
    table = _DevToolsTable()
    module.ENVIRONMENT_NAME = "dev"
    module.DEV_TOOLS_CONTROLS_TABLE_NAME = "controls"
    module.dynamodb = _DynamoResource(table)
    admin = {"cognito:groups": ["admins"], "email": "admin@example.com"}

    created = module.handler(
        _event(
            "/api/admin/dev-tools/failure-plans",
            "POST",
            admin,
            {"workflow_id": "recovery-demo-1", "step": "log_lookup", "ttl_minutes": 15},
        ),
        None,
    )
    assert created["statusCode"] == 201
    control = json.loads(created["body"])["control"]
    assert control["status"] == "ACTIVE"

    listed = module.handler(_event("/api/admin/dev-tools", "GET", admin), None)
    assert listed["statusCode"] == 200
    assert json.loads(listed["body"])["controls"][0]["workflow_id"] == "recovery-demo-1"

    disabled = module.handler(
        _event(f"/api/admin/dev-tools/failure-plans/{control['control_id']}/disable", "POST", admin, {}),
        None,
    )
    assert json.loads(disabled["body"])["control"]["status"] == "DISARMED"

    forbidden = module.handler(_event("/api/admin/dev-tools", "GET", {"cognito:groups": ["operators"]}), None)
    assert forbidden["statusCode"] == 403
