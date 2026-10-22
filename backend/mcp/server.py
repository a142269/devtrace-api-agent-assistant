"""Minimal MCP (Model Context Protocol) server over JSON-RPC 2.0.

Implements initialize / tools/list / tools/call, plus a stdio entrypoint
(``python -m mcp.server``) so the same tools can be hosted as a real MCP
process. The stdio wire format is UTF-8 newline-delimited JSON: the entrypoint
reconfigures stdin/stdout itself, so Chinese tool text survives a Windows
locale codec without the caller having to set ``PYTHONUTF8``. The server is
transport-agnostic: ``handle`` takes a JSON-RPC request dict and returns a
JSON-RPC response dict (or None for notifications).
"""
import asyncio
import inspect
import json
import sys
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"


class MCPToolError(Exception):
    pass


class MCPServer:
    def __init__(self, name: str = "devtrace-mcp", version: str = "1.0.0"):
        self.name = name
        self.version = version
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, description: str, input_schema: dict,
                 handler: Callable[[dict], Any]) -> None:
        self._tools[name] = {"name": name, "description": description,
                             "inputSchema": input_schema, "handler": handler}

    def tool_list(self) -> List[dict]:
        return [{key: tool[key] for key in ("name", "description", "inputSchema")}
                for tool in self._tools.values()]

    async def handle(self, request: dict) -> Optional[dict]:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._error(request.get("id"), -32600, "Invalid Request")
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}
        if request_id is None:
            return None  # notification, e.g. notifications/initialized
        if method == "initialize":
            return self._result(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": self.version},
            })
        if method == "tools/list":
            return self._result(request_id, {"tools": self.tool_list()})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            tool = self._tools.get(name)
            if not tool:
                return self._error(request_id, -32602, f"Unknown tool: {name}")
            try:
                value = tool["handler"](arguments)
                if inspect.isawaitable(value):
                    value = await value
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                return self._result(request_id, {
                    "content": [{"type": "text", "text": text}], "isError": False})
            except Exception as exc:  # noqa: BLE001 - MCP tool boundary
                return self._result(request_id, {
                    "content": [{"type": "text", "text": str(exc)}], "isError": True})
        return self._error(request_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _result(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": message}}


def build_default_server(runtime: Any = None) -> MCPServer:
    """Host the read-only tools; ``create_support_ticket`` stays local on purpose.

    A ticket needs explicit user authorization plus the conversation and idempotency
    context that only the server holds, so it must not become an MCP tool parameter.
    """
    from developer.planner import TOOL_SCHEMAS, TOOL_TITLES
    from mcp.runtime import build_runtime_from_env

    active = runtime if runtime is not None else build_runtime_from_env()

    async def search_api_docs(arguments):
        return await active.search_api_docs(arguments.get("query", ""),
                                            top_k=arguments.get("top_k", 3))

    async def search_quota_docs(arguments):
        return await active.search_quota_docs(arguments.get("query", ""),
                                              top_k=arguments.get("top_k", 3))

    async def get_request_trace(arguments):
        return await active.get_request_trace(arguments.get("request_id", ""))

    async def get_related_request_trace(arguments):
        return await active.get_related_request_trace(arguments.get("request_id", ""))

    async def inspect_key_scope(arguments):
        return await active.inspect_key_scope(arguments.get("key_prefix", ""))

    async def get_webhook_delivery(arguments):
        return await active.get_webhook_delivery(arguments.get("delivery_id", ""))

    async def search_public_docs(arguments):
        return await active.search_public_docs(arguments.get("query", ""), api=arguments.get("api", ""),
                                               top_k=arguments.get("top_k", 4))

    async def lookup_error(arguments):
        return await active.lookup_error_playbook(arguments.get("symptom", ""), api=arguments.get("api", ""),
                                                  http_status=arguments.get("http_status"),
                                                  top_k=arguments.get("top_k", 3))

    server = MCPServer()
    handlers = {
        "search_api_docs": search_api_docs,
        "search_quota_docs": search_quota_docs,
        "get_request_trace": get_request_trace,
        "get_related_request_trace": get_related_request_trace,
        "inspect_key_scope": inspect_key_scope,
        "get_webhook_delivery": get_webhook_delivery,
        "search_public_docs": search_public_docs,
        "lookup_error_playbook": lookup_error,
    }
    for name, handler in handlers.items():
        server.register(name, TOOL_TITLES[name], TOOL_SCHEMAS[name], handler)
    return server


def force_utf8_stdio(*streams: Any) -> List[str]:
    """Return the streams switched to UTF-8; streams without ``reconfigure`` are skipped.

    On Windows a redirected stdio gets the locale codec (cp936), which would write the
    Chinese tool descriptions as bytes a UTF-8 client cannot decode.
    """
    reconfigured: List[str] = []
    for stream in streams:
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError, OSError):
            continue
        reconfigured.append(type(stream).__name__)
    return reconfigured


async def _stdio_loop() -> None:
    """Serve newline-delimited JSON-RPC over stdin/stdout.

    Lines are read on a worker thread because the Windows proactor loop cannot attach
    ``connect_read_pipe`` to a redirected stdin handle, which would hang the process.
    """
    force_utf8_stdio(sys.stdin, sys.stdout)
    server = build_default_server()
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
        if not line:
            break
        try:
            request = json.loads(line.decode("utf-8"))
        except ValueError:
            response = MCPServer._error(None, -32700, "Parse error")
        else:
            response = await server.handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(_stdio_loop())
