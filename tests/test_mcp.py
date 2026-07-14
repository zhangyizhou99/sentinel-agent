"""Tests for the from-scratch MCP server (dispatch level). | MCP 服务器测试。

Run: PYTHONPATH=src pytest tests/ -q
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.mcp_server import _handle  # noqa: E402


def test_initialize():
    r = _handle("initialize", {})
    assert r["serverInfo"]["name"] == "sentinel"
    assert "tools" in r["capabilities"] and "resources" in r["capabilities"]


def test_tools_list_has_all_verbs():
    names = {t["name"] for t in _handle("tools/list", {})["tools"]}
    assert {"discover", "instrument", "query", "alerts", "export",
            "dashboard", "feedback", "deploy_alerts", "apply"} <= names


def test_tool_schema_marks_required():
    tools = {t["name"]: t for t in _handle("tools/list", {})["tools"]}
    schema = tools["deploy_alerts"]["inputSchema"]
    assert "repo_path" in schema["required"]
    assert "contact_point" in schema["required"]
    assert "prune" not in schema["required"]        # has a default
    assert schema["properties"]["prune"]["type"] == "boolean"


def test_resource_templates_listed():
    tmpls = {t["uriTemplate"] for t in _handle("resources/templates/list", {})["resourceTemplates"]}
    assert "sentinel://catalog/{repo}" in tmpls
    assert "sentinel://memory/{repo}" in tmpls


def test_static_knowledge_resource_reads():
    r = _handle("resources/read", {"uri": "sentinel://knowledge/observability"})
    data = json.loads(r["contents"][0]["text"])
    assert any(p["signal"] == "errors" for p in data)


def test_unknown_tool_is_error():
    r = _handle("tools/call", {"name": "does_not_exist", "arguments": {}})
    assert r.get("isError") is True


def test_unknown_method_raises():
    with pytest.raises(ValueError):
        _handle("bogus/method", {})
