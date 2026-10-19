"""Real dependencies behind the MCP read tools.

``mcp.server`` stays transport-only and imports this lazily, so ``python -m mcp.server``
can answer ``tools/list`` before ChromaDB or the simulated platform sandbox is reachable.
A missing dependency surfaces as a per-tool ``isError`` instead of a crashed server.
"""

import os
import re
from typing import Any, Callable, Dict, List, Optional, Union

import httpx

from developer.error_playbooks import lookup_playbook
from mcp.server import MCPToolError

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
VERSION_RE = re.compile(r"^(v[12])\b", re.I)
KIND_RE = re.compile(r"^v[12]\s+(auth|quota|webhook)\b", re.I)
DOC_KIND_MARKERS = {"auth": "鉴权", "quota": "速率限制", "webhook": "Webhook"}
KNOWLEDGE_METHODS = ("search", "search_async", "search_hybrid_async")


def _identifier(value: Any, label: str) -> str:
    """Reject anything that could escape the sandbox mock path."""
    text = str(value or "").strip()
    if not IDENTIFIER_RE.fullmatch(text):
        raise MCPToolError(f"{label} 不是合法标识符")
    return text


def _positive_int(value: Any, default: int, maximum: int = 20) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return default if number <= 0 else min(number, maximum)


async def search_platform_docs(knowledge: Any, query: str, top_k: int = 3,
                               version: Optional[str] = None) -> List[Dict[str, Any]]:
    """Version-filtered platform retrieval shared by the local and MCP tool paths."""
    match = VERSION_RE.match(query)
    resolved = (version or (match.group(1).lower() if match else "v1")).lower()
    if hasattr(knowledge, "search_hybrid_async"):
        results = await knowledge.search_hybrid_async(query, top_k=4, version=resolved)
    else:
        results = await knowledge.search_async(query, top_k=4)
    kind_match = KIND_RE.match(query)
    if not kind_match:
        return list(results)[:top_k]
    marker = DOC_KIND_MARKERS[kind_match.group(1).lower()]
    return [item for item in results if marker.lower() in str(item.get("title", "")).lower()]


class MCPToolRuntime:
    """Knowledge base + simulated sandbox HTTP behind the eight read tools.

    ``knowledge`` and ``public_knowledge`` accept either an instance or a factory so a
    stdio process can register tools without warming ChromaDB at startup.
    """

    def __init__(self, *, knowledge: Union[Any, Callable[[], Any], None] = None,
                 sandbox_url: str = "", public_knowledge: Union[Any, Callable[[], Any], None] = None,
                 timeout: float = 10.0):
        self._knowledge = knowledge
        self._public_knowledge = public_knowledge
        self.sandbox_url = sandbox_url.rstrip("/")
        self.timeout = timeout

    def _resolve(self, provider: Any, label: str) -> Any:
        if provider is None:
            raise MCPToolError(f"{label}未配置")
        if any(hasattr(provider, method) for method in KNOWLEDGE_METHODS):
            return provider
        if callable(provider):
            return provider()
        raise MCPToolError(f"{label}配置无效")

    def knowledge(self) -> Any:
        return self._resolve(self._knowledge, "平台文档知识库")

    def public_knowledge(self) -> Any:
        return self._resolve(self._public_knowledge, "公开文档知识库")

    async def _sandbox_get(self, path: str) -> Dict[str, Any]:
        if not self.sandbox_url:
            raise MCPToolError("模拟平台沙箱地址未配置")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.sandbox_url + path)
            response.raise_for_status()
            return response.json()

    async def search_api_docs(self, query: str = "", top_k: int = 3) -> List[Dict[str, Any]]:
        return await search_platform_docs(self.knowledge(), str(query or ""),
                                          _positive_int(top_k, 3))

    async def search_quota_docs(self, query: str = "", top_k: int = 3) -> List[Dict[str, Any]]:
        return await search_platform_docs(self.knowledge(), str(query or ""),
                                          _positive_int(top_k, 3))

    async def get_request_trace(self, request_id: str = "") -> Dict[str, Any]:
        return await self._sandbox_get("/mock/requests/" + _identifier(request_id, "request_id"))

    async def get_related_request_trace(self, request_id: str = "") -> Dict[str, Any]:
        return await self._sandbox_get("/mock/requests/" + _identifier(request_id, "request_id"))

    async def inspect_key_scope(self, key_prefix: str = "") -> Dict[str, Any]:
        return await self._sandbox_get("/mock/keys/" + _identifier(key_prefix, "key_prefix"))

    async def get_webhook_delivery(self, delivery_id: str = "") -> Dict[str, Any]:
        return await self._sandbox_get("/mock/webhooks/" + _identifier(delivery_id, "delivery_id"))

    async def search_public_docs(self, query: str = "", api: str = "",
                                 top_k: int = 4) -> List[Dict[str, Any]]:
        knowledge = self.public_knowledge()
        return knowledge.search(str(query or ""), api=str(api or ""),
                                top_k=_positive_int(top_k, 4))

    async def lookup_error_playbook(self, symptom: str = "", api: str = "",
                                    http_status: Any = None,
                                    top_k: int = 3) -> List[Dict[str, Any]]:
        status = str(http_status or "").strip()
        return lookup_playbook(str(symptom or ""), api=str(api or ""),
                               http_status=int(status) if status else None,
                               top_k=_positive_int(top_k, 3))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def build_runtime_from_env() -> MCPToolRuntime:
    """Wire the runtime from process environment for the standalone stdio server."""

    def knowledge_factory():
        from developer.data import DOCS
        from mcp.knowledge_base import KnowledgeBase
        return KnowledgeBase(
            chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
            chroma_port=_env_int("CHROMA_PORT", 8000),
            chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
            collection_name="developer_knowledge", default_documents=DOCS,
        )

    def public_factory():
        from developer.public_knowledge import PublicDocsKnowledge
        return PublicDocsKnowledge()

    return MCPToolRuntime(
        knowledge=knowledge_factory,
        public_knowledge=public_factory,
        sandbox_url=os.getenv("DEVELOPER_SANDBOX_URL", "http://developer-sandbox:8100"),
    )
