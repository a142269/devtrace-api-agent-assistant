"""Minimal MCP (Model Context Protocol) server over JSON-RPC 2.0.

Implements initialize / tools/list / tools/call, plus a stdio entrypoint
(``python -m mcp.server``) so the same tools can be hosted as a real MCP
process. The server is transport-agnostic: ``handle`` takes a JSON-RPC request
dict and returns a JSON-RPC response dict (or None for notifications).
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


def build_default_server() -> MCPServer:
    from developer.error_playbooks import lookup_playbook
    from developer.planner import TOOL_SCHEMAS
    from developer.public_knowledge import PublicDocsKnowledge

    knowledge = PublicDocsKnowledge()

    def search_public_docs(arguments):
        return knowledge.search(arguments.get("query", ""), api=arguments.get("api", ""),
                                top_k=arguments.get("top_k", 4))

    def lookup_error(arguments):
        status = arguments.get("http_status")
        return lookup_playbook(arguments.get("symptom", ""), api=arguments.get("api", ""),
                               http_status=int(status) if status else None,
                               top_k=arguments.get("top_k", 3))

    server = MCPServer()
    server.register("search_public_docs", "Search public API documentation summaries",
                    TOOL_SCHEMAS["search_public_docs"], search_public_docs)
    server.register("lookup_error_playbook", "Look up error remediation playbooks",
                    TOOL_SCHEMAS["lookup_error_playbook"], lookup_error)
    return server


async def _stdio_loop() -> None:
    server = build_default_server()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while True:
        line = await reader.readline()
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
