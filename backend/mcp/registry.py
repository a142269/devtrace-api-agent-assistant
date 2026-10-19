"""Optional external MCP servers, connected over stdio and registered as read tools.

Configuration is a JSON array in ``DEVTRACE_MCP_SERVERS``::

    [{"name": "example", "command": "python", "args": ["server.py"]}]

Nothing here is trusted. A third-party server is an external process that can return
arbitrary text, so two rules bound the blast radius:

* **Default deny.** A tool is registered only when its name or description reads like a
  lookup and it carries no write hint. Unrecognisable tools are skipped.
* **No server-held parameters.** Identifiers and credentials the platform owns
  (user, conversation, idempotency key, ticket id, API key) are stripped from the tool
  schema and re-filtered on every call, so a server cannot ask the agent to leak them.

The agent's own tools never fall back to these servers, and an unavailable server is
skipped at startup rather than retried mid-conversation.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from mcp.client import MCPClient, StdioTransport
from mcp.tool_manager import Tool

logger = logging.getLogger(__name__)

SERVER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Only these fields are read from a configuration entry; anything else is ignored.
ENTRY_FIELDS = ("name", "command", "args")

READ_HINTS = re.compile(
    r"\b(search|lookup|look\s?up|find|get|fetch|query|read|list|show|describe|inspect|"
    r"retrieve|check|view|detail|details|status|summary|doc|docs|documentation)\b",
    re.I,
)
WRITE_HINTS = re.compile(
    r"\b(create|post|delete|submit|send|update|patch|write|insert|remove|modify|assign|"
    r"cancel|refund|pay|purchase|buy|transfer|approve|reject|execute|run|deploy|start|"
    r"stop|restart|trigger|enable|disable|upload|import|sync|ticket|order|webhook|"
    r"comment|reply|message|notify|merge|close|open|publish)\b",
    re.I,
)
# Parameters the platform owns. A server must never receive or request them.
SERVER_HELD_PARAMS = ("user_id", "conversation_id", "idempotency_key", "ticket_id", "api_key")


def parse_servers_config(raw: str) -> List[Dict[str, Any]]:
    """Return validated stdio entries. Invalid input means "no external servers"."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        logger.warning("DEVTRACE_MCP_SERVERS 不是合法 JSON，已跳过全部外部 MCP 服务器")
        return []
    if not isinstance(payload, list):
        logger.warning("DEVTRACE_MCP_SERVERS 需要 JSON 数组，已跳过全部外部 MCP 服务器")
        return []
    entries: List[Dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or set(item) - set(ENTRY_FIELDS):
            logger.warning("忽略缺少 name/command/args 字段的外部 MCP 配置项: %r", item)
            continue
        name = str(item.get("name") or "").strip().lower()
        command = str(item.get("command") or "").strip()
        args = item.get("args") or []
        if not SERVER_NAME_RE.fullmatch(name):
            logger.warning("忽略非法的外部 MCP 服务器名: %r", name)
            continue
        if not command or not isinstance(args, list):
            logger.warning("忽略缺少 command 或 args 的外部 MCP 服务器: %s", name)
            continue
        entries.append({"name": name, "command": command,
                        "args": [str(arg) for arg in args][:20]})
    return entries


def tool_hints(name: str, description: str, schema: Dict[str, Any]) -> Tuple[str, ...]:
    """Classify a server tool: 'read', 'write' or 'unknown', plus write hints found."""
    text = f"{name} {description}"
    for param in (schema or {}).get("properties", {}):
        if WRITE_HINTS.search(str(param)):
            return "write", ("parameter",)
    write_matches = WRITE_HINTS.findall(text)
    if write_matches:
        return "write", tuple(sorted({m.lower() for m in write_matches}))
    if READ_HINTS.search(text):
        return "read", ()
    return "unknown", ()


def sanitize_schema(input_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Drop server-held parameters and rebuild a strict object schema."""
    schema = input_schema if isinstance(input_schema, dict) else {}
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    kept = {key: (value if isinstance(value, dict) else {"type": "string"})
            for key, value in properties.items()
            if str(key) not in SERVER_HELD_PARAMS}
    required = [key for key in (schema.get("required") or []) if key in kept]
    return {"type": "object", "properties": kept, "required": required,
            "additionalProperties": False}


def tool_name(server: str, tool: str) -> str:
    return f"ext_{server}_{tool}"


class ExternalMCPServers:
    """Connected external servers and the tools they contributed to the manager."""

    def __init__(self) -> None:
        self.clients: List[Tuple[str, MCPClient]] = []
        self.registered: List[str] = []
        self.skipped: List[Dict[str, str]] = []
        self.servers: List[Dict[str, Any]] = []

    @property
    def tool_names(self) -> List[str]:
        return list(self.registered)

    async def connect(self, manager: Any, raw_config: str,
                     reserved: Optional[List[str]] = None,
                     timeout_s: float = 12.0) -> Dict[str, Any]:
        """Connect every configured server and register only its read tools."""
        reserved_names = {str(name) for name in (reserved or [])}
        for entry in parse_servers_config(raw_config):
            client = MCPClient(StdioTransport(entry["command"], entry["args"]))
            try:
                await client.initialize()
                tools = await client.list_tools()
            except Exception as exc:
                self.skipped.append({"server": entry["name"], "reason": f"unavailable: {exc}"[:160]})
                logger.warning("外部 MCP 服务器 %s 连接失败，已跳过：%s", entry["name"], str(exc)[:160])
                await self._discard(client)
                continue
            self.clients.append((entry["name"], client))
            accepted = 0
            for tool in tools:
                decision = self._register(manager, client, entry["name"], tool,
                                          reserved_names, timeout_s)
                if decision:
                    accepted += 1
            self.servers.append({**entry, "discovered": len(tools), "registered": accepted})
            logger.info("外部 MCP 服务器 %s：%s 个工具中发现 %s 个只读工具",
                        entry["name"], len(tools), accepted)
        return self.summary()

    def _register(self, manager: Any, client: MCPClient, server: str, tool: Any,
                  reserved_names: set, timeout_s: float) -> bool:
        if not isinstance(tool, dict):
            return False
        name = str(tool.get("name") or "").strip()
        description = str(tool.get("description") or "")
        raw_schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        if not name or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name):
            self.skipped.append({"server": server, "tool": name[:64], "reason": "invalid_name"})
            return False
        public = tool_name(server, name)
        taken = reserved_names | set(getattr(manager, "tool_names", []))
        if public in taken:
            self.skipped.append({"server": server, "tool": public, "reason": "name_conflict"})
            logger.warning("外部 MCP 工具 %s 与内置工具同名，已跳过", public)
            return False
        kind, hints = tool_hints(name, description, raw_schema)
        if kind != "read":
            self.skipped.append({"server": server, "tool": public,
                                 "reason": f"not_read_only:{kind}"})
            logger.warning("外部 MCP 工具 %s 未通过只读白名单（%s%s），已跳过",
                           public, kind, f":{','.join(hints)}" if hints else "")
            return False
        schema = sanitize_schema(raw_schema)
        allowed_fields = tuple(schema["properties"])
        if not allowed_fields:
            self.skipped.append({"server": server, "tool": public, "reason": "no_allowed_params"})
            logger.warning("外部 MCP 工具 %s 没有可转发的只读参数，已跳过", public)
            return False

        def make_handler(server_tool: str, fields: Tuple[str, ...], active: MCPClient):
            # params is schema-checked against the sanitized schema, but forward the
            # declared fields explicitly so nothing else can reach the server.
            async def handler(params, _context):
                arguments = {key: params[key] for key in fields if key in params}
                return await active.call_tool(server_tool, arguments)
            return handler

        manager.register(Tool(name=public, description=description[:500] or public,
                              handler=make_handler(name, allowed_fields, client),
                              schema=schema, cache_ttl=0, timeout_s=timeout_s))
        self.registered.append(public)
        return True

    async def close(self) -> None:
        for _, client in self.clients:
            await self._discard(client)
        self.clients = []

    @staticmethod
    async def _discard(client: MCPClient) -> None:
        try:
            await client.close()
        except Exception:
            logger.warning("关闭外部 MCP 客户端失败", exc_info=True)

    def summary(self) -> Dict[str, Any]:
        return {
            "servers": [{**{k: s[k] for k in ENTRY_FIELDS},
                         "discovered": s["discovered"], "registered": s["registered"]}
                        for s in self.servers],
            "registered_tools": list(self.registered),
            "skipped": list(self.skipped),
        }


async def connect_external_servers(manager: Any, raw_config: str,
                                   reserved: Optional[List[str]] = None) -> ExternalMCPServers:
    servers = ExternalMCPServers()
    if not (raw_config or "").strip():
        return servers
    await servers.connect(manager, raw_config, reserved=reserved)
    return servers
