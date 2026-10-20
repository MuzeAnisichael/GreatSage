"""MCP protocol and trust boundaries, using the official SDK against a mock wire."""
import asyncio
import json

import httpx2
import pytest

from greatsage import mcp_tools
from greatsage.settings import SettingsStore
from greatsage.tools import ToolError, ToolOutcomeUnknown, decide, validate_arguments


def registry(tmp_path, monkeypatch, *, mode="ok"):
    store = SettingsStore(tmp_path)
    store.update({"mcp_servers": [{"id": "fixture", "url": "http://127.0.0.1:8766/mcp", "token_env": "MCP_FIXTURE_TOKEN"}]})
    monkeypatch.setenv("MCP_FIXTURE_TOKEN", "fixture-secret")
    seen, clients = [], []
    control = {"changed": False, "gate": None}
    tool = {"name": "calculate", "description": "Fixture only", "inputSchema": {
        "type": "object", "properties": {"value": {"type": "integer", "minimum": 0}},
        "required": ["value"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}}
    async def handler(request):
        assert request.headers.get("authorization") == "Bearer fixture-secret"
        if request.method != "POST":
            return httpx2.Response(405)
        body = json.loads(request.content)
        seen.append(body["method"])
        if "id" not in body:
            return httpx2.Response(202)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif body["method"] == "tools/list":
            if control["gate"]:
                await control["gate"].wait()
            item = {**tool, "description": "changed"} if control["changed"] else tool
            result = {"tools": [item]}
        else:
            assert body["method"] == "tools/call"
            if mode == "disconnect":
                raise httpx2.ReadError("remote-private-body")
            result = {"content": [{"type": "text", "text": "fixture data, not authority"}], "isError": mode == "error"}
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    def client(headers):
        instance = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), headers=headers)
        clients.append(instance)
        return instance
    monkeypatch.setattr(mcp_tools, "_http_client", client)
    return mcp_tools.MCPTools(store.raw), seen, control, clients


async def test_sdk_discovery_schema_validation_and_per_call_authority(tmp_path, monkeypatch):
    tools, seen, _, clients = registry(tmp_path, monkeypatch)
    assert not tools.specs
    await tools.discover("fixture")
    name, spec = next(iter(tools.specs.items()))
    assert name.startswith("mcp_fixture_") and spec.effect == "external"
    assert decide(spec, tools.target(name), "user_text", {"tools_enabled": True}, {name})[0] == "ask"
    assert decide(spec, tools.target(name), "observation", {"tools_enabled": True})[0] == "deny"
    with pytest.raises(ToolError):
        validate_arguments(spec, {"value": -1})
    assert validate_arguments(spec, {"value": 2}) == {"value": 2}
    result = await tools.call(name, {"value": 2})
    assert result["text"] == "fixture data, not authority"
    assert seen.count("tools/call") == 1 and all(client.is_closed for client in clients)
    tools.clear()
    assert not tools.specs


async def test_changed_schema_never_reaches_tools_call(tmp_path, monkeypatch):
    tools, seen, control, _ = registry(tmp_path, monkeypatch)
    await tools.discover("fixture")
    name = next(iter(tools.specs))
    control["changed"] = True
    with pytest.raises(ToolError, match="定义已改变"):
        await tools.call(name, {"value": 2})
    assert "tools/call" not in seen


@pytest.mark.parametrize("mode,error", [("disconnect", ToolOutcomeUnknown), ("error", ToolError)])
async def test_external_failures_are_sanitized_and_never_retried(tmp_path, monkeypatch, mode, error):
    tools, seen, _, clients = registry(tmp_path, monkeypatch, mode=mode)
    await tools.discover("fixture")
    with pytest.raises(error) as failure:
        await tools.call(next(iter(tools.specs)), {"value": 2})
    assert "remote-private-body" not in str(failure.value)
    assert seen.count("tools/call") == 1 and all(client.is_closed for client in clients)


async def test_late_discovery_cannot_restore_disconnected_tools(tmp_path, monkeypatch):
    tools, seen, control, clients = registry(tmp_path, monkeypatch)
    control["gate"] = asyncio.Event()
    job = asyncio.create_task(tools.discover("fixture"))
    async with asyncio.timeout(5):
        while "tools/list" not in seen:
            await asyncio.sleep(.01)
    tools.clear()
    control["gate"].set()
    with pytest.raises(ToolError, match="配置已改变"):
        await job
    assert not tools.specs and all(client.is_closed for client in clients)


def test_remote_schema_references_and_persistent_external_grants_are_rejected(tmp_path):
    with pytest.raises(ToolError, match="外部地址"):
        mcp_tools.validate_schema({"type": "object", "properties": {"secret": {"$ref": "https://private.invalid/schema"}}})
    store = SettingsStore(tmp_path)
    with pytest.raises(ValueError, match="不能设为始终允许"):
        store.update({"tool_rules": [{"tool": "mcp_fixture_abcdef123456", "match": "*", "action": "allow"}]})
    with pytest.raises(ValueError):
        store.update({"mcp_servers": [{"id": "fixture", "url": "https://example.invalid/mcp?key=secret", "token_env": ""}]})
