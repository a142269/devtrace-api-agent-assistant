"""Minimal MCP client: JSON-RPC 2.0 over an in-process server or stdio transport."""
import asyncio
import json
import os
import sys
from typing import Any, Optional

from mcp.server import MCPServer, MCPToolError


class MCPError(Exception):
    pass


class InProcessTransport:
    """Talk to an MCPServer without spawning a process (default for local demo)."""

    def __init__(self, server: MCPServer):
        self.server = server

    async def request(self, payload: dict) -> dict:
        response = await self.server.handle(payload)
        return response if response is not None else {}

    async def notify(self, payload: dict) -> None:
        await self.server.handle(payload)

    async def close(self) -> None:
        return None


class StdioTransport:
    """Spawn a child process and exchange one UTF-8 JSON object per line on its stdio.

    With no command it starts the bundled server (`python -m mcp.server`); a registry
    entry supplies its own executable and arguments for third-party servers.
    """

    def __init__(self, command: Optional[str] = None, args: Optional[list] = None,
                 cwd: Optional[str] = None):
        self.command = [command, *[str(arg) for arg in (args or [])]] if command else \
            [sys.executable, "-m", "mcp.server"]
        self.cwd = cwd or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._process = None
        self._lock = asyncio.Lock()

    def _child_env(self) -> dict:
        """Force UTF-8 in the child: an inherited ``PYTHONUTF8=0`` is overridden by design.

        Without it a Windows child writes the Chinese tool text in cp936 and the
        ``readline`` + ``decode("utf-8")`` below raises UnicodeDecodeError.
        """
        return {**os.environ, 'PYTHONUTF8': '1'}

    async def start(self) -> None:
        if self._process is None:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=self.cwd,
                env=self._child_env())

    async def request(self, payload: dict) -> dict:
        await self.start()
        async with self._lock:
            assert self._process and self._process.stdin and self._process.stdout
            self._process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await self._process.stdin.drain()
            if "id" not in payload:
                return {}
            line = await asyncio.wait_for(self._process.stdout.readline(), timeout=30)
        if not line:
            raise MCPError("MCP server closed the connection")
        return json.loads(line.decode("utf-8"))

    async def notify(self, payload: dict) -> None:
        await self.request(payload)

    async def close(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            await self._process.wait()


class MCPClient:
    def __init__(self, transport: Any):
        self.transport = transport
        self._next_id = 0
        self.server_info: dict = {}

    async def _request(self, method: str, params: Optional[dict] = None,
                       notification: bool = False) -> dict:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if notification:
            notify = getattr(self.transport, "notify", None)
            if notify is not None:
                await notify(payload)
            else:
                await self.transport.request(payload)
            return {}
        self._next_id += 1
        payload["id"] = self._next_id
        response = await self.transport.request(payload)
        if "error" in response:
            raise MCPError(response["error"].get("message", "MCP error"))
        return response.get("result") or {}

    async def initialize(self) -> dict:
        result = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "devtrace", "version": "1.0.0"},
        })
        self.server_info = result
        await self._request("notifications/initialized", notification=True)
        return result

    async def list_tools(self):
        return (await self._request("tools/list")).get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> Any:
        try:
            result = await self._request("tools/call", {"name": name, "arguments": arguments})
        except MCPError as exc:
            raise MCPToolError(str(exc)) from exc
        if result.get("isError"):
            content = result.get("content") or []
            message = content[0].get("text") if content else "MCP tool failed"
            raise MCPToolError(message)
        content = result.get("content") or []
        if not content:
            return None
        text = content[0].get("text", "")
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text

    async def close(self) -> None:
        await self.transport.close()
