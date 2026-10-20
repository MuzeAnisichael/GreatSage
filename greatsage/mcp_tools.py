"""Explicit Streamable HTTP MCP discovery and permission-gated tool execution.

Sessions are scoped to one operation. The SDK owns protocol, cancellation and
transport cleanup; no MCP server can sample a model, obtain roots or grant tools.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager

import httpx2
from jsonschema import Draft202012Validator
from referencing import Registry

from .settings import read_env
from .tools import ToolError, ToolOutcomeUnknown, ToolSpec

MAX_TOOLS = 32


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _controlled_error(error):
    if isinstance(error, ToolError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            if found := _controlled_error(child):
                return found
    return None


def validate_schema(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object" or len(json.dumps(schema)) > 32_000:
        raise ToolError("MCP 参数定义必须是大小受限的 JSON 对象。")
    def visit(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if key in ("$ref", "$dynamicRef") and (not isinstance(value, str) or not value.startswith("#")):
                    raise ToolError("MCP 参数定义不能引用外部地址。")
                visit(value)
        elif isinstance(item, list):
            for value in item:
                visit(value)
    try:
        visit(schema)
        Draft202012Validator.check_schema(schema)
    except ToolError:
        raise
    except Exception:
        raise ToolError("MCP 参数定义无效。") from None


def validate_mcp_arguments(spec, arguments):
    try:
        if len(json.dumps(arguments)) > 64_000:
            raise ValueError()
        Draft202012Validator(spec.parameters, registry=Registry()).validate(arguments)
    except Exception:
        raise ToolError("MCP 工具参数不符合已发现的定义。") from None
    return arguments


def _http_client(headers):
    return httpx2.AsyncClient(headers=headers, timeout=30, follow_redirects=False)


@asynccontextmanager
async def sdk_session(config):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    key = read_env(config["token_env"]) if config["token_env"] else ""
    if config["token_env"] and not key:
        raise ToolError("MCP 服务的凭据环境变量尚未配置。")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with _http_client(headers) as client:
        async with streamable_http_client(config["url"], http_client=client, max_sse_event_size=1_000_000) as streams:
            async with ClientSession(*streams, read_timeout_seconds=30) as session:
                await session.initialize()
                yield session


class MCPTools:
    def __init__(self, settings, session_factory=sdk_session):
        self.settings, self.session_factory = settings, session_factory
        self.entries = {}
        self.epoch = 0
        # Raw SDK diagnostic frames may contain remote text. The application
        # reports sanitized failures through its normal task/API audit instead.
        logger = logging.getLogger("mcp")
        if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
            logger.addHandler(logging.NullHandler())
        logger.propagate = False

    def config(self, server_id):
        for item in self.settings()["mcp_servers"]:
            if item["id"] == server_id:
                return item
        raise ToolError("MCP 服务不存在或已移除。")

    def clear(self):
        self.epoch += 1
        self.entries.clear()

    @property
    def specs(self):
        configured = {row["id"]: row for row in self.settings()["mcp_servers"]}
        return {name: entry["spec"] for name, entry in self.entries.items()
                if configured.get(entry["server"]["id"]) == entry["server"]}

    def status(self):
        return [{**row, "transport": "streamable_http", "tools": [
            {"name": name, "title": spec.title, "effect": "external"} for name, spec in self.specs.items()
            if self.entries[name]["server"]["id"] == row["id"]]} for row in self.settings()["mcp_servers"]]

    async def _listing(self, session):
        from mcp.types import PaginatedRequestParams
        result, cursor, seen = [], None, set()
        for _ in range(8):
            page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
            result.extend(page.tools)
            if len(result) > MAX_TOOLS:
                raise ToolError(f"单个 MCP 服务最多接入 {MAX_TOOLS} 个工具，请在服务端缩小工具集。")
            cursor = page.next_cursor
            if not cursor:
                return result
            if cursor in seen:
                break
            seen.add(cursor)
        raise ToolError("MCP 工具目录分页异常。")

    @staticmethod
    def _entry(server, tool):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool.name):
            raise ToolError("MCP 工具名称无效。")
        validate_schema(tool.input_schema)
        descriptor = tool.model_dump(mode="json", exclude_none=True)
        name = f"mcp_{server['id']}_{_digest(tool.name)[:12]}"
        spec = ToolSpec(name, f"{server['id']} · {tool.title or tool.name}"[:120], "external",
                        f"External MCP tool {tool.name}. Requires the user's confirmation for each call. "
                        + (tool.description or "")[:1000], tool.input_schema)
        return {"server": dict(server), "remote_name": tool.name, "spec": spec,
                "version": _digest({"server": server, "tool": descriptor})}

    async def discover(self, server_id):
        config, epoch = self.config(server_id), self.epoch
        try:
            async with asyncio.timeout(35):
                async with self.session_factory(config) as session:
                    entries = [self._entry(config, tool) for tool in await self._listing(session)]
            if epoch != self.epoch or self.config(server_id) != config:
                raise ToolError("MCP 配置已改变，请重新发现工具。")
            self.entries = {name: item for name, item in self.entries.items() if item["server"]["id"] != server_id}
            self.entries.update({entry["spec"].name: entry for entry in entries})
            return self.status()
        except ToolError:
            raise
        except Exception as exc:
            if controlled := _controlled_error(exc):
                raise controlled from None
            raise ToolError("MCP 工具发现失败，请检查地址、凭据与服务状态。") from None

    def target(self, name):
        if name not in self.specs:
            raise ToolError("MCP 工具尚未发现或配置已改变。")
        entry = self.entries[name]
        return f"{entry['server']['id']}/{entry['remote_name']}@{entry['version']}"

    async def call(self, name, arguments):
        self.target(name)
        entry = self.entries[name]
        validate_mcp_arguments(entry["spec"], arguments)
        dispatched = False
        try:
            async with asyncio.timeout(65):
                async with self.session_factory(entry["server"]) as session:
                    tools = await self._listing(session)
                    tool = next((tool for tool in tools if tool.name == entry["remote_name"]), None)
                    if not tool or self._entry(entry["server"], tool)["version"] != entry["version"]:
                        raise ToolError("MCP 工具定义已改变，请重新发现工具后发起新请求。")
                    if name not in self.specs or self.entries.get(name) != entry:
                        raise ToolError("MCP 配置已改变，操作已停止。")
                    dispatched = True
                    result = await session.call_tool(entry["remote_name"], arguments, read_timeout_seconds=45)
                    if result.is_error:
                        raise ToolError("MCP 工具返回错误；该次调用不会自动重试。")
                    # Text and structured output are data, never local instructions.
                    output = {"text": "\n".join(block.text for block in result.content if block.type == "text")[:20_000],
                              "structured": result.structured_content}
                    if len(json.dumps(output, ensure_ascii=False)) > 64_000:
                        output = {"text": output["text"], "truncated": True}
                    return output
        except ToolError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if controlled := _controlled_error(exc):
                raise controlled from None
            if dispatched:
                raise ToolOutcomeUnknown("MCP 调用已发出但结果未知，请检查外部服务；不会自动重试。") from None
            raise ToolError("MCP 连接或定义校验失败，尚未执行工具。") from None
