"""Plan → action → observation developer assistant, isolated from customer support."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, Optional

import httpx
from anthropic import AsyncAnthropic

from mcp.runtime import search_platform_docs
from mcp.tool_manager import MCPToolManager, Tool
from memory.conversation_memory import MsgRole

logger = logging.getLogger(__name__)
model_metrics = contextvars.ContextVar('developer_model_metrics', default=None)

from developer.error_playbooks import lookup_playbook
from developer import audit
from developer.planner import (KIND_TO_TOOLS, MCP_READ_TOOLS, MCP_ROUTED_KINDS,
                               PUBLIC_API_MCP_TOOLS, TOOL_EVIDENCE, TOOL_SCHEMAS, TOOL_TITLES,
                               _approved_tools, _ready_steps, base_tool, build_plan_candidates,
                               choose_plan_candidate, mcp_name)
from developer.public_docs import detect_public_api
from developer.prompts import PROMPT_VERSION, render
from developer.security import scan_observation, scan_untrusted, tainted_sources
from developer.ttl import redis_ttl_seconds


IDENTIFIER_RE = re.compile(r"\b(req_[A-Za-z0-9_-]+|delivery_[A-Za-z0-9_-]+)\b", re.I)
TICKET_RE = re.compile(
    r"(?:请|帮我|麻烦|现在|确认|同意|我要|我想|我需要|想|需要)\s*(?:给我)?\s*(?:创建|提交|开)(?:一张|一个|个)?工单(?:[。！!，,\s]|$)"
    r"|(?:^|[。！!，,;；\s])(?:创建|提交|开)(?:一张|一个|个)?工单(?:[。！!，,\s]|$)"
    r"|(?:please\s+)?create (?:a )?ticket(?: for me| now)?(?:[.! ,]|$)", re.I,
)
TICKET_CANCEL_RE = re.compile(r"(?:不要|别|不用|不需|无需|暂不|先不|禁止).{0,8}(?:创建|提交|开).{0,4}工单|(?:do not|don't|never) create (?:a )?ticket", re.I)


def _redact(value: str) -> str:
    """Keep user-provided credentials out of model prompts, traces and memory."""
    value = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(api[_ -]?key\s*[:=]\s*)\S+", r"\1[REDACTED]", value)
    return re.sub(r"\b(?:sk-|whsec_)[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)


def _untrusted_text(value: Any) -> str:
    """Keep instruction-like text from untrusted records out of user-facing text."""
    if not isinstance(value, str):
        return '' if value is None else str(value)
    if scan_untrusted(value):
        return '记录中的疑似指令内容已按数据处理'
    return value


def _json_text(blocks: Any) -> Dict[str, Any]:
    # Some Anthropic-compatible endpoints return a separate thinking block with
    # text=None. Parse each text block independently so it cannot be mixed with
    # the final answer or exposed through the public timeline.
    for block in reversed(blocks or []):
        raw = getattr(block, "text", None) or ""
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start < 0 or end <= start:
            continue
        try:
            result = json.loads(raw[start:end])
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
    raise ValueError("模型没有返回 JSON 对象")


def _kind(message: str, identifier: str = "") -> str:
    text = (message + " " + identifier).lower()
    if identifier.lower().startswith("delivery_"):
        return "webhook"
    if identifier.lower().startswith("req_quota_"):
        return "quota"
    if identifier.lower().startswith("req_auth_"):
        return "auth"
    if detect_public_api(message) or ("公开" in text and "文档" in text):
        return "public_api"
    if "delivery_" in text or "webhook" in text or "回调" in text or "投递" in text:
        return "webhook"
    if "quota" in text or "429" in text or "限流" in text or "配额" in text:
        return "quota"
    return "auth"


class RuleDecisionModel:
    """Deterministic reference model for offline tests and optional local demo."""

    async def make_plan(self, message: str, memory: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        kind = previous.get("kind") if previous and previous.get("status") == "waiting" else _kind(message)
        goal = "查询公开 API 文档" if kind == "public_api" else f"排查{kind}接入问题"
        return {"kind": kind, "goal": goal, "tools": KIND_TO_TOOLS[kind]}

    async def choose_action(self, state: Dict[str, Any], message: str) -> Dict[str, Any]:
        pending = next(iter(state.get("ready_steps", _ready_steps(state))), None)
        if not pending:
            return {"action": "finish"}
        tool = pending["tool"]
        issue_id = state.get("issue_id", "")
        status_match = re.search(r'\b([45]\d\d)\b', message)
        playbook_args = {"symptom": message[:300], "api": detect_public_api(message)}
        if status_match:
            playbook_args["http_status"] = int(status_match.group(1))
        args = {
            "search_api_docs": {"query": f"{state.get('api_version', 'v1')} {state['kind']} {message[:100]}", "top_k": 3},
            "search_public_docs": {"query": message[:300], "api": detect_public_api(message)},
            "lookup_error_playbook": playbook_args,
            "get_request_trace": {"request_id": issue_id},
            "inspect_key_scope": {"key_prefix": state.get("key_prefix", "")},
            "get_webhook_delivery": {"delivery_id": issue_id},
            "get_related_request_trace": {"request_id": state.get("related_request_id", "")},
            "search_quota_docs": {"query": "v1 quota 429", "top_k": 3},
            "create_support_ticket": {"issue_id": issue_id},
        }[base_tool(tool)]
        return {"action": "tool", "step_id": pending["id"], "tool": tool, "args": args}

    async def revise_plan(self, state: Dict[str, Any], failed_tool: str) -> Dict[str, Any]:
        return {"ask_user": "模拟平台没有找到这条记录。请核对并提供正确的 request_id 或 delivery_id。"}

    async def summarize(self, state: Dict[str, Any], memory: str) -> str:
        observations = [item for item in state["actions"] if item["success"]]
        if not observations:
            return "目前缺少可核验的记录。请提供 request_id 或 delivery_id，我会继续排查。"
        lines = []
        for item in observations:
            data = item.get("result")
            # Evidence text is keyed by base name so an MCP-routed step reads the same.
            tool = base_tool(item["tool"])
            if tool in {"search_api_docs", "search_quota_docs"} and isinstance(data, list):
                titles = [str(doc.get("title")) for doc in data if isinstance(doc, dict) and doc.get("title")]
                if titles:
                    lines.append("已核对模拟平台文档：" + "、".join(titles) + "。")
            elif tool == "get_request_trace" and isinstance(data, dict):
                lines.append(f"请求 {data.get('request_id')} 返回 {data.get('status')}：{_untrusted_text(data.get('error_summary'))}。")
                if data.get("retry_after_seconds") is not None:
                    lines.append(f"请等待 Retry-After {data['retry_after_seconds']} 秒后再重试。")
            elif tool == "get_related_request_trace" and isinstance(data, dict):
                lines.append(f"关联请求 {data.get('request_id')} 返回 {data.get('status')}：{_untrusted_text(data.get('error_summary'))}。")
            elif tool == "inspect_key_scope" and isinstance(data, dict):
                lines.append(f"密钥前缀 {data.get('key_prefix')} 的状态为 {data.get('status')}，scope 为 {', '.join(data.get('scopes', []))}。")
            elif tool == "get_webhook_delivery" and isinstance(data, dict):
                lines.append(f"投递 {data.get('delivery_id')} 返回 {data.get('status')}：{_untrusted_text(data.get('error_summary'))}。")
            elif tool == "create_support_ticket" and isinstance(data, dict):
                lines.append(f"已创建模拟工单 {data.get('ticket_id')}。")
            elif tool == "search_public_docs" and isinstance(data, list):
                for doc in data[:3]:
                    if isinstance(doc, dict):
                        lines.append(
                            f"参考公开文档《{doc.get('title')}》（{doc.get('source_url')}）：{doc.get('content')}")
            elif tool == "lookup_error_playbook" and isinstance(data, list):
                for entry in data[:2]:
                    if not isinstance(entry, dict):
                        continue
                    retry = "可重试" if entry.get("retryable") else "不可重试"
                    lines.append(
                        f"错误处置：{entry.get('title')}（{retry}，来源 {entry.get('source_url')}）")
                    if entry.get("retry_policy"):
                        lines.append(f"退避策略：{entry['retry_policy']}。")
                    if entry.get("recovery"):
                        lines.append("处理建议：" + "；".join(entry["recovery"]) + "。")
        delivery = next((a['result'] for a in observations if base_tool(a['tool']) == 'get_webhook_delivery'
                         and isinstance(a.get('result'), dict)), None)
        related = next((a['result'] for a in observations if base_tool(a['tool']) == 'get_related_request_trace'
                        and isinstance(a.get('result'), dict)), None)
        if delivery and related and delivery.get('status') == 503 and related.get('status') == 429:
            lines.append('建议先检查 Webhook 接收端可用性，同时按关联请求记录的等待时间控制重试；'
                         '这两条记录有关联，但不足以证明限流导致投递失败。')
        suffix = ("以上内容来自公开 API 文档摘要，不连接真实平台；模拟平台数据会另行标注。"
                  if state.get("kind") == "public_api"
                  else "以上仅基于本地模拟平台数据；如需排查真实系统，请提供真实平台可用的脱敏记录。")
        return "\n".join(lines) + "\n" + suffix


class AnthropicDecisionModel(RuleDecisionModel):
    """Model-backed planning and action selection with a safe deterministic fallback."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None, skill: str = ""):
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncAnthropic(**kwargs)
        self.model = model
        self.skill = skill[:4000]
        self.native_tools_disabled = False

    def _system_prompt(self) -> str:
        return render("system", skill=self.skill)

    async def _create(self, *, prompt: Optional[str] = None,
                      messages: Optional[list] = None, tools: Optional[list] = None):
        started = time.perf_counter()
        metrics = model_metrics.get()
        if metrics is not None:
            metrics['calls'] += 1
        payload = {
            "model": self.model, "max_tokens": 4096, "temperature": 0,
            "system": self._system_prompt(),
            "messages": messages or [{"role": "user", "content": prompt or ""}],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = {"type": "auto"}
        try:
            response = await self.client.messages.create(**payload)
        except Exception:
            if metrics is not None:
                metrics['errors'] += 1
            raise
        finally:
            if metrics is not None:
                metrics['latency_ms'] += (time.perf_counter() - started) * 1000
        usage = getattr(response, 'usage', None)
        if metrics is not None and usage is not None:
            for name in ('input_tokens', 'output_tokens'):
                value = getattr(usage, name, None)
                if isinstance(value, int):
                    metrics[name] = (metrics[name] or 0) + value
        return response

    async def _ask(self, prompt: str) -> Dict[str, Any]:
        response = await self._create(prompt=prompt)
        try:
            return _json_text(response.content)
        except ValueError:
            logger.warning("模型响应缺少有效 JSON: stop_reason=%s, text_lengths=%s",
                           response.stop_reason,
                           [len(getattr(block, "text", None) or "") for block in response.content])
            raise

    async def _choose_native_action(self, state: Dict[str, Any], message: str):
        """Let the model call one of the approved tools natively.

        Returns None when the provider does not emit a tool_use block, allowing
        the JSON-prompt fallback. Disabled for the process after a provider error
        so an incompatible endpoint does not pay the failed call every turn.
        """
        if self.native_tools_disabled or not getattr(self, 'client', None):
            return None
        ready = state.get("ready_steps", _ready_steps(state))
        tools = []
        for step in ready:
            schema = TOOL_SCHEMAS.get(step["tool"])
            if not schema:
                continue
            tools.append({
                "name": step["tool"],
                "description": f"{TOOL_TITLES[step['tool']]}；所需证据：{TOOL_EVIDENCE[step['tool']]}",
                "input_schema": schema,
            })
        if not tools:
            return None
        response = await self._create(
            messages=[{"role": "user", "content": render(
                "native_action",
                state=json.dumps(state, ensure_ascii=False, default=str)[:5000],
                message=message[:500])}],
            tools=tools,
        )
        block = next((item for item in (response.content or [])
                      if getattr(item, 'type', None) == 'tool_use'), None)
        if block is None:
            return None
        selected = next((step for step in ready if step["tool"] == getattr(block, 'name', None)), None)
        if selected is None:
            return None
        args = dict(getattr(block, 'input', None) or {})
        required = TOOL_SCHEMAS[selected["tool"]].get("required", [])
        if not all(key in args for key in required):
            return None
        metrics = model_metrics.get()
        if metrics is not None:
            metrics['native_tool_calls'] = metrics.get('native_tool_calls', 0) + 1
        return {"action": "tool", "step_id": selected["id"],
                "tool": selected["tool"], "args": args}

    async def make_plan(self, message: str, memory: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # A named third-party API is a documentation question, not the mock platform path.
        public_api = detect_public_api(message)
        if public_api:
            return {"kind": "public_api", "goal": f"查询 {public_api} 公开 API 文档",
                    "tools": ["lookup_error_playbook", "search_public_docs"]}
        fallback = await super().make_plan(message, memory, previous)
        try:
            result = await self._ask(render(
                "plan", message=message[:1000], memory=memory[:1200]))
            kind = result.get("kind")
            tools = result.get("tools")
            if kind in KIND_TO_TOOLS and isinstance(tools, list):
                proposed = [tool for tool in tools[:10] if isinstance(tool, str)]
                if proposed:
                    return {"kind": kind, "goal": str(result.get("goal") or fallback["goal"])[:160], "tools": proposed}
        except Exception as ex:
            logger.warning("开发者计划生成失败，使用保守计划 (HTTP %s)", getattr(ex, "status_code", "unknown"))
        return {**fallback, "_fallback": True}

    async def choose_action(self, state: Dict[str, Any], message: str) -> Dict[str, Any]:
        fallback = await super().choose_action(state, message)
        if fallback["action"] == "finish":
            return fallback
        try:
            native = await self._choose_native_action(state, message)
            if native:
                return native
        except Exception as ex:
            # A provider without tool-use support should not fail the turn; remember
            # it for this process and use the JSON prompt path instead.
            self.native_tools_disabled = True
            logger.warning("原生工具选择不可用，回退 JSON 提示 (HTTP %s)", getattr(ex, "status_code", "unknown"))
        try:
            result = await self._ask(render(
                "action",
                state=json.dumps(state, ensure_ascii=False, default=str)[:5000],
                message=message[:500]))
            ready = state.get("ready_steps", _ready_steps(state))
            selected = next((step for step in ready if step["id"] == result.get("step_id")
                             and step["tool"] == result.get("tool")), None)
            if (selected and result.get("action") == "tool" and isinstance(result.get("args"), dict)):
                # The model may omit required fields; fall back to the rule-built args
                # instead of executing a call that is guaranteed to fail validation.
                required = {"lookup_error_playbook": {"symptom"},
                            "search_public_docs": {"query"}}.get(base_tool(selected["tool"]), set())
                if required.issubset(result["args"]):
                    return result
                fallback = await super().choose_action(state, message)
                if fallback.get("action") == "tool" and fallback.get("tool") == selected["tool"]:
                    return fallback
                return result
        except Exception as ex:
            logger.warning("开发者行动选择失败，按计划执行 (HTTP %s)", getattr(ex, "status_code", "unknown"))
        return {**fallback, "_fallback": True}

    async def revise_plan(self, state: Dict[str, Any], failed_tool: str) -> Dict[str, Any]:
        try:
            result = await self._ask(render(
                "revise", failed_tool=failed_tool,
                state=json.dumps(state, ensure_ascii=False, default=str)[:4000]))
            if isinstance(result.get("ask_user"), str) and result["ask_user"].strip():
                return {"ask_user": result["ask_user"].strip()[:240]}
        except Exception:
            pass
        return {**(await super().revise_plan(state, failed_tool)), "_fallback": True}


class DeveloperAssistant:
    MAX_ACTIONS = 5

    def __init__(self, *, memory: Any, knowledge: Any, redis_client: Any, model: Any,
                 tool_manager: MCPToolManager, sandbox_url: str, dynamic_scope: bool = True,
                 public_knowledge: Any = None, mcp_client: Any = None,
                 mcp_public_tools: bool = False, mcp_all_tools: bool = False,
                 planning_candidates: bool = False):
        self.memory = memory
        self.knowledge = knowledge
        self.public_knowledge = public_knowledge
        self.mcp_client = mcp_client
        self.mcp_public_tools = bool(mcp_public_tools and mcp_client is not None)
        self.mcp_all_tools = bool(mcp_all_tools and mcp_client is not None)
        self.planning_candidates = bool(planning_candidates)
        self.redis = redis_client
        self.model = model
        self.tools = tool_manager
        self.sandbox_url = sandbox_url.rstrip("/")
        self.dynamic_scope = dynamic_scope
        self._register_tools()

    def _register_tools(self) -> None:
        async def docs(params, _context):
            return await search_platform_docs(self.knowledge, params["query"],
                                              params.get("top_k", 3))

        async def public_docs(params, _context):
            if self.public_knowledge is None:
                raise RuntimeError('公开文档知识库未配置')
            return self.public_knowledge.search(
                params["query"], api=params.get("api", ""), top_k=params.get("top_k", 4))

        async def playbook(params, _context):
            http_status = params.get("http_status")
            return lookup_playbook(params["symptom"], api=params.get("api", ""),
                                   http_status=int(http_status) if http_status else None,
                                   top_k=params.get("top_k", 3))

        async def mcp_public_docs(params, _context):
            return await self.mcp_client.call_tool("search_public_docs", {
                "query": params["query"], "api": params.get("api", ""),
                "top_k": params.get("top_k", 4)})

        async def mcp_playbook(params, _context):
            arguments = {"symptom": params["symptom"], "api": params.get("api", ""),
                         "top_k": params.get("top_k", 3)}
            if params.get("http_status"):
                arguments["http_status"] = int(params["http_status"])
            return await self.mcp_client.call_tool("lookup_error_playbook", arguments)

        async def sandbox_get(path: str):
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(self.sandbox_url + path)
                response.raise_for_status()
                return response.json()

        async def request(params, _context):
            return await sandbox_get("/mock/requests/" + params["request_id"])

        async def key(params, _context):
            return await sandbox_get("/mock/keys/" + params["key_prefix"])

        async def webhook(params, _context):
            return await sandbox_get("/mock/webhooks/" + params["delivery_id"])

        async def ticket(params, context):
            payload = {
                "idempotency_key": context["idempotency_key"],
                "user_id": context["user_id"],
                "conversation_id": context["conversation_id"],
                "issue_id": params["issue_id"],
                "summary": context["summary"],
            }
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(self.sandbox_url + "/mock/tickets", json=payload)
                response.raise_for_status()
                return response.json()

        definitions = [
            ("search_api_docs", docs, {"query": "string", "top_k": "integer"}, ["query"], 300),
            ("get_request_trace", request, {"request_id": "string"}, ["request_id"], 0),
            ("inspect_key_scope", key, {"key_prefix": "string"}, ["key_prefix"], 0),
            ("get_webhook_delivery", webhook, {"delivery_id": "string"}, ["delivery_id"], 0),
            ("get_related_request_trace", request, {"request_id": "string"}, ["request_id"], 0),
            ("search_quota_docs", docs, {"query": "string", "top_k": "integer"}, ["query"], 300),
            ("search_public_docs", public_docs, {"query": "string", "api": "string", "top_k": "integer"}, ["query"], 0),
            ("lookup_error_playbook", playbook,
             {"symptom": "string", "api": "string", "http_status": "integer", "top_k": "integer"},
             ["symptom"], 0),
            ("create_support_ticket", ticket, {"issue_id": "string"}, ["issue_id"], 0),
        ]
        if self.mcp_public_tools:
            definitions.extend([
                ("mcp_search_public_docs", mcp_public_docs,
                 {"query": "string", "api": "string", "top_k": "integer"}, ["query"], 0),
                ("mcp_lookup_error_playbook", mcp_playbook,
                 {"symptom": "string", "api": "string", "http_status": "integer", "top_k": "integer"},
                 ["symptom"], 0),
            ])
        for name, handler, props, required, ttl in definitions:
            self.tools.register(Tool(
                name=name, description=TOOL_TITLES[name], handler=handler,
                schema={"type": "object", "properties": {key: {"type": value} for key, value in props.items()},
                        "required": required, "additionalProperties": False},
                cache_ttl=ttl, timeout_s=12,
            ))
        if self.mcp_all_tools:
            self._register_mcp_read_tools()

    def _register_mcp_read_tools(self) -> None:
        """Register the ``mcp_*`` twins of the read tools, served over the MCP client.

        Only declared read-only fields are forwarded. A failed MCP call is recorded as
        a failed step rather than silently retried on the local tool, so the audit
        shows the transport that was actually used.
        """
        def mcp_handler(tool_name: str, fields: tuple):
            async def handler(params, _context):
                arguments = {field: params[field] for field in fields if params.get(field) not in (None, "")}
                return await self.mcp_client.call_tool(tool_name, arguments)
            return handler

        docs_tools = {"search_api_docs", "search_quota_docs"}
        for base in MCP_READ_TOOLS:
            name = mcp_name(base)
            schema = TOOL_SCHEMAS[name]
            self.tools.register(Tool(
                name=name, description=TOOL_TITLES[name],
                handler=mcp_handler(base, tuple(schema["properties"])),
                schema=schema, cache_ttl=300 if base in docs_tools else 0, timeout_s=12,
            ))

    @staticmethod
    def _state_key(user_id: str, conversation_id: str) -> str:
        digest = hashlib.sha256(f"{user_id}\0{conversation_id}".encode()).hexdigest()
        return f"developer:plan:{digest}"

    async def run(self, message: str, user_id: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        started = time.perf_counter()
        metrics = {'calls': 0, 'errors': 0, 'latency_ms': 0.0, 'input_tokens': None,
                   'output_tokens': None, 'native_tool_calls': 0}
        model_metrics.set(metrics)
        message = _redact(message)
        security_flags = scan_untrusted(message, 'user', 'user_message')
        user_id = user_id.strip()[:120] or "anonymous"
        conversation_id = conversation_id or str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        state_key = self._state_key(user_id, conversation_id)
        raw = await self.redis.get(state_key)
        previous = json.loads(raw) if raw else None
        memory = await self.memory.get_context(user_id, conversation_id, query=message)
        memory_text = memory.to_prompt_text()
        found = IDENTIFIER_RE.search(message)
        issue_id = found.group(1).lower() if found else (previous or {}).get("issue_id", "")
        if previous and previous.get("status") == "waiting" and issue_id:
            state = previous
            state["status"] = "running"
            state.setdefault("timeline", []).append({"type": "resume", "text": "读取上轮未完成计划和已取得的证据"})
            for step in state["steps"]:
                if step["tool"] is None and step["status"] == "waiting":
                    step["status"] = "done"
                elif step["status"] in {"waiting", "failed"}:
                    step["status"] = "pending"
        else:
            plan = await self.model.make_plan(message, memory_text, None)
            kind = (_kind(message, issue_id) if issue_id else
                    plan.get("kind") if plan.get("kind") in KIND_TO_TOOLS else _kind(message))
            allowed_override = None
            if self.mcp_public_tools and kind == 'public_api':
                allowed_override = PUBLIC_API_MCP_TOOLS
            elif self.mcp_all_tools and kind in MCP_ROUTED_KINDS:
                allowed_override = [mcp_name(tool) for tool in KIND_TO_TOOLS[kind]]
            proposed, chosen, adjustments = _approved_tools(kind, plan.get("tools"), allowed_override)
            chosen_bases = [base_tool(tool) for tool in chosen]
            if issue_id.startswith('req_auth_v2_') and 'get_request_trace' in chosen_bases \
                    and 'search_api_docs' in chosen_bases:
                tool = chosen.pop(chosen_bases.index('get_request_trace'))
                chosen.insert(0, tool)
                adjustments.append('先核验请求记录中的 API 版本，再检索对应文档')
            if plan.get("kind") != kind:
                adjustments.append("根据用户提供的问题标识修正故障类型")
            plan_candidates = []
            if self.planning_candidates:
                plan_candidates = build_plan_candidates(
                    kind, chosen, allowed_override, issue_id,
                    require_version=issue_id.startswith('req_auth_v2_'))
                selected = choose_plan_candidate(plan_candidates)
                if selected != chosen:
                    adjustments.append("候选计划评分：选择 " + " → ".join(selected))
                chosen = selected
            state = {
                "kind": kind, "goal": str(plan.get("goal") or f"排查 {kind} 接入问题")[:160],
                "proposed_tools": proposed, "approved_tools": chosen,
                "plan_candidates": plan_candidates,
                "plan_adjustments": adjustments,
                "issue_id": issue_id, "key_prefix": "", "api_version": "v1",
                "require_version_observation": issue_id.startswith('req_auth_v2_'), "status": "running",
                "steps": [{"id": f"s{i+1}", "title": TOOL_TITLES[tool], "tool": tool,
                           "required_evidence": TOOL_EVIDENCE[tool], "status": "pending"}
                          for i, tool in enumerate(chosen)],
                "actions": [], "evidence": [], "replans": 0, "ticket_id": None,
                "ticket_authorized": False,
                "model_fallback": bool(plan.get("_fallback")),
                "timeline": [{"type": "plan", "text": "计划提议：" + (" → ".join(proposed) or "无有效工具提议")},
                             {"type": "approval", "text": "批准执行：" + " → ".join(chosen)}],
            }
            for adjustment in adjustments:
                state["timeline"].append({"type": "approval", "text": adjustment})
            if state["model_fallback"]:
                state["timeline"].append({"type": "fallback", "text": "模型规划不可用，已采用规则计划"})
        if issue_id:
            state["issue_id"] = issue_id
        if issue_id.startswith('req_auth_v2_'):
            state['require_version_observation'] = True
            if state.get('api_version', 'v1') != 'v2':
                for step in state['steps']:
                    if base_tool(step['tool']) == 'search_api_docs' and step['status'] == 'done':
                        step['status'] = 'pending'
                        state['actions'] = [a for a in state['actions']
                                            if base_tool(a['tool']) != 'search_api_docs']
                        state['evidence'] = [e for e in state['evidence']
                                             if base_tool(e.split(':', 1)[0]) not in
                                             ('search_api_docs',) and not e.startswith('doc:')]
                        state['timeline'].append({'type': 'revision', 'text': '标识显示可能是 v2，移除未确认版本的文档证据并重新核验'})
        if security_flags:
            state['timeline'].append({
                'type': 'security',
                'text': '用户消息包含疑似指令表述，已按不可信数据处理，未作为工具指令执行',
            })
        from developer.events import publish_existing
        state['timeline'] = publish_existing(state['timeline'])
        ticket_cancelled = bool(TICKET_CANCEL_RE.search(message))
        # A quoted example is not an instruction to create a ticket.
        unquoted_message = re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"|\'[^\']*\'', '', message)
        ticket_requested_now = bool(TICKET_RE.search(unquoted_message)) and not ticket_cancelled
        if ticket_requested_now and not any(step["tool"] == "create_support_ticket" for step in state["steps"]):
            state["steps"].append({"id": f"s{len(state['steps'])+1}", "title": TOOL_TITLES["create_support_ticket"],
                                   "tool": "create_support_ticket", "required_evidence": TOOL_EVIDENCE["create_support_ticket"],
                                   "status": "pending"})
            state["timeline"].append({"type": "plan", "text": "用户已提出创建模拟工单，计划增加工单步骤"})
        if ticket_requested_now:
            state["ticket_authorized"] = True
        if ticket_cancelled:
            state["ticket_authorized"] = False
            for step in state["steps"]:
                if step["tool"] == "create_support_ticket" and step["status"] in {"pending", "waiting"}:
                    step["status"] = "cancelled"
            state["timeline"].append({"type": "revision", "text": "用户取消创建工单，已停止工单步骤"})
        ticket_requested = state.get("ticket_authorized", False)
        question = ""
        actions_this_turn = 0

        def _variant(base_name: str) -> str:
            """Keep a dynamically appended step on the transport the plan already uses."""
            routed = any(step["tool"] != base_tool(step["tool"])
                         and base_tool(step["tool"]) in MCP_READ_TOOLS for step in state["steps"])
            return mcp_name(base_name) if routed else base_name

        while actions_this_turn < self.MAX_ACTIONS:
            # A checkpoint represents a resumable state if a worker or stream
            # stops before the next step is recorded. Ticket creation has its
            # own business idempotency key for the side-effect boundary.
            await self.redis.setex(state_key, redis_ttl_seconds(),
                                   json.dumps({**state, 'status': 'waiting'}, ensure_ascii=False))
            pending_steps = [step for step in state["steps"] if step["status"] == "pending"]
            if not pending_steps:
                state["status"] = "complete"
                break
            ready = _ready_steps(state)
            if not ready:
                blocked = pending_steps[0]
                blocked_base = base_tool(blocked["tool"])
                blocked["status"] = "waiting"
                if blocked_base == "create_support_ticket" and not ticket_requested:
                    question = "如需创建模拟支持工单，请明确回复“创建工单”。"
                elif not state["issue_id"]:
                    question = "请提供模拟平台的 request_id（如 req_auth_01）或 delivery_id（如 delivery_01）。"
                elif blocked_base == "inspect_key_scope" and not state.get("key_prefix"):
                    question = "还需要密钥前缀才能核对权限，请提供脱敏 key_prefix。"
                else:
                    question = "仍缺少可核验的证据，请核对问题标识后继续。"
                state["status"] = "waiting"
                break
            decision_state = {**state, "ready_steps": ready}
            if state.get("model_fallback") and isinstance(self.model, AnthropicDecisionModel):
                decision = await RuleDecisionModel.choose_action(self.model, decision_state, message)
            else:
                decision = await self.model.choose_action(decision_state, message)
            if decision.get("_fallback"):
                state["model_fallback"] = True
                state["timeline"].append({"type": "fallback", "text": "模型行动选择不可用，按已验证的规则计划执行"})
            pending = next((step for step in ready if step["id"] == decision.get("step_id")
                            and step["tool"] == decision.get("tool")), None)
            if decision.get("action") != "tool" or pending is None:
                state["status"] = "waiting"
                question = "模型未选择可执行步骤，请发送“继续”重试。"
                break
            tool = pending["tool"]
            base = base_tool(tool)
            state["timeline"].append({"type": "selection", "text": f"选择 {pending['id']}：{TOOL_TITLES[tool]}", "step_id": pending["id"]})
            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            # Identifiers come from observed records or user input, never solely from the model.
            # The MCP twins take the same server-derived arguments; nothing here is sent
            # to the MCP server beyond the read-only fields its schema declares.
            if base == "get_request_trace":
                args = {"request_id": state["issue_id"]}
            elif base == "get_related_request_trace":
                args = {"request_id": state["related_request_id"]}
            elif base == "search_quota_docs":
                args = {"query": "v1 quota 429", "top_k": 3}
            elif base == "get_webhook_delivery":
                args = {"delivery_id": state["issue_id"]}
            elif base == "inspect_key_scope":
                args = {"key_prefix": state["key_prefix"]}
            elif base == "create_support_ticket":
                args = {"issue_id": state["issue_id"]}
            elif base == "search_api_docs":
                args = {"query": f"{state.get('api_version', 'v1')} {state['kind']} {message[:120]}", "top_k": 3}
            context = {
                "user_id": user_id, "conversation_id": conversation_id,
                "idempotency_key": hashlib.sha256(f"{user_id}\0{conversation_id}\0{state['issue_id']}".encode()).hexdigest(),
                "summary": f"{state['goal']}；已核验来源：{', '.join(state['evidence'])}"[:1000],
            }
            state["timeline"].append({"type": "action", "text": f"开始：{TOOL_TITLES[tool]}", "step_id": pending["id"]})
            result = await self.tools.call(tool, args, context=context,
                                           use_cache=base in {"search_api_docs", "search_quota_docs"})
            actions_this_turn += 1
            data = result.data if result.success else None
            source = f"{tool}:{args.get('request_id') or args.get('delivery_id') or args.get('key_prefix') or state.get('api_version', 'v1')}"
            observation_flags = scan_observation(data, 'observation', source) if data else []
            if observation_flags:
                security_flags.extend(observation_flags)
                flagged_steps = {event.get('step_id') for event in state['timeline']
                                 if event.get('type') == 'security'}
                if pending['id'] not in flagged_steps:
                    state['timeline'].append({
                        'type': 'security', 'step_id': pending['id'],
                        'text': '工具观察包含疑似指令内容，已按不可信数据处理，不会据此执行工具',
                    })
            action = {"step_id": pending["id"], "tool": tool, "input": args, "success": bool(result.success and data),
                      "summary": self._observation_summary(tool, data, result.error), "source": source,
                      "result": data, "error": result.error or "", "latency_ms": round(result.latency_ms, 1),
                      "tainted": bool(observation_flags)}
            # Audit carries the tool outcome only — never the observation payload itself.
            await audit.record(self.redis, 'tool.call', actor=user_id, resource=run_id,
                               status='success' if action['success'] else 'failure',
                               detail={'tool': tool, 'success': action['success'],
                                       'latency_ms': action['latency_ms']})
            state["actions"].append(action)
            state["timeline"].append({"type": "observation", "text": action["summary"], "step_id": pending["id"]})
            pending["status"] = "done" if result.success and data else "failed"
            state["timeline"].append({"type": "status", "text": f"{pending['id']} → {pending['status']}", "step_id": pending["id"]})
            if pending["status"] == "done":
                state["evidence"].append(source)
                if base in {"search_api_docs", "search_quota_docs"}:
                    state["evidence"].extend(
                        f"doc:{doc['title']}" for doc in data
                        if isinstance(doc, dict) and doc.get("title")
                    )
                if base == 'get_webhook_delivery' and isinstance(data, dict):
                    related = data.get('related_request_id', '')
                    if isinstance(related, str) and re.fullmatch(r'req_[A-Za-z0-9_-]+', related):
                        state['related_request_id'] = related
                        if not any(base_tool(item['tool']) == 'get_related_request_trace' for item in state['steps']):
                            related_step = _variant('get_related_request_trace')
                            state['steps'].append({'id': f"s{len(state['steps']) + 1}",
                                'title': TOOL_TITLES[related_step], 'tool': related_step,
                                'required_evidence': TOOL_EVIDENCE[related_step], 'status': 'pending'})
                            state['approved_tools'].append(related_step)
                            state['timeline'].append({'type': 'decision', 'text': '投递记录包含关联请求，追加核验请求记录'})
                if base == 'get_related_request_trace' and isinstance(data, dict) and data.get('status') == 429:
                    if not any(base_tool(item['tool']) == 'search_quota_docs' for item in state['steps']):
                        quota_step = _variant('search_quota_docs')
                        state['steps'].append({'id': f"s{len(state['steps']) + 1}",
                            'title': TOOL_TITLES[quota_step], 'tool': quota_step,
                            'required_evidence': TOOL_EVIDENCE[quota_step], 'status': 'pending'})
                        state['approved_tools'].append(quota_step)
                        state['timeline'].append({'type': 'decision', 'text': '关联请求返回 429，追加限流文档核验'})
                if base == "get_request_trace" and isinstance(data, dict):
                    state["key_prefix"] = data.get("key_prefix", "")
                    if data.get('api_version') in {'v1', 'v2'}:
                        state['api_version'] = data['api_version']
                    if (self.dynamic_scope and state['kind'] == 'auth' and data.get('reason_code') in {'expired_key', 'missing_scope'}
                            and not any(base_tool(item['tool']) == 'inspect_key_scope' for item in state['steps'])):
                        scope_name = _variant('inspect_key_scope')
                        new_step = {'id': f"s{len(state['steps']) + 1}", 'title': TOOL_TITLES[scope_name],
                                    'tool': scope_name, 'required_evidence': TOOL_EVIDENCE[scope_name], 'status': 'pending'}
                        state['steps'].append(new_step)
                        state['approved_tools'].append(scope_name)
                        state['timeline'].append({'type': 'decision', 'text': '请求记录显示需核对密钥状态或权限，追加检查', 'step_id': new_step['id']})
                    # A verified malformed header already explains the rejection. Scope
                    # inspection is useful only when that observation does not settle it.
                    if self.dynamic_scope and state['kind'] == 'auth' and data.get('reason_code') in {'malformed_header', 'missing_api_key_header'}:
                        for candidate in state['steps']:
                            if base_tool(candidate['tool']) == 'inspect_key_scope' and candidate['status'] == 'pending':
                                candidate['status'] = 'skipped'
                                state['timeline'].append({'type': 'decision', 'text': '请求记录已确认认证头格式错误，跳过权限检查', 'step_id': candidate['id']})
                if base == "create_support_ticket" and isinstance(data, dict):
                    state["ticket_id"] = data.get("ticket_id")
            else:
                error_text = str(result.error or '').lower()
                missing_record = base in {'get_request_trace', 'get_webhook_delivery'} and '404' in error_text
                if missing_record:
                    recovery_question = '模拟平台没有找到这条记录，请核对并提供正确的问题标识。'
                elif base in {'search_api_docs', 'search_quota_docs'} and result.success:
                    recovery_question = '未找到匹配的文档证据，请检查知识库后重试。'
                else:
                    recovery_question = '工具暂不可用或结果不足，请稍后发送“继续”重试；当前不推断根因。'
                if state["replans"] < 1:
                    state["replans"] += 1
                    if state.get("model_fallback") and isinstance(self.model, AnthropicDecisionModel):
                        revision = await RuleDecisionModel.revise_plan(self.model, state, tool)
                    else:
                        revision = await self.model.revise_plan(state, tool)
                    if revision.get("_fallback"):
                        state["model_fallback"] = True
                    # Only a verified 404 should suggest that a user supplied identifier is wrong.
                    question = str(revision.get("ask_user") or recovery_question)[:240] if missing_record else recovery_question
                    state["steps"].append({
                        "id": f"s{len(state['steps'])+1}", "title": "补充标识并恢复计划" if missing_record else "等待工具恢复并继续计划",
                        "tool": None, "required_evidence": "用户提供可查询的问题标识" if missing_record else "工具恢复或文档可用", "status": "waiting",
                    })
                    state["timeline"].append({"type": "revision", "text": "计划调整：" + recovery_question})
                else:
                    question = recovery_question
                state["status"] = "waiting"
                break
        if state["status"] == "running":
            if any(step["status"] == "pending" for step in state["steps"]):
                state["status"] = "waiting"
                question = "已达到本轮工具调用上限，请发送“继续”完成余下步骤。"
            else:
                state["status"] = "complete"
        from developer.verifier import build_diagnosis
        diagnosis = build_diagnosis(state['kind'], state['actions'])
        answer = await self.model.summarize(state, memory_text)
        if question:
            answer = answer + "\n" + question
        if diagnosis['conflicts']:
            answer += "\n证据冲突：" + "；".join(item['explanation'] for item in diagnosis['conflicts']) + "。"
        for linkage in diagnosis['linkages']:
            # The correlated case already has its caveat in the deterministic summary.
            if linkage['assessment'] == 'correlated_unproven':
                continue
            answer += "\n" + linkage['explanation'] + "。"
        if state["kind"] == "public_api":
            if "公开 API 文档" not in answer:
                answer += "\n以上内容来自公开 API 文档摘要，不连接真实平台。"
        elif "模拟平台" not in answer:
            answer += "\n以上基于本地模拟平台数据。"
        if state.get("model_fallback"):
            answer += "\n本轮模型决策未完成，计划或行动选择使用规则降级。"
        decision_mode = ("rules_fallback" if state.get("model_fallback") else
                         "model" if isinstance(self.model, AnthropicDecisionModel) else "rules")
        response = {
            "conversation_id": conversation_id, "run_id": run_id, "answer": answer,
            "kind": state["kind"], "plan": {"goal": state["goal"], "status": state["status"],
                                        "steps": state["steps"], "replans": state["replans"],
                                        "proposed_tools": state.get("proposed_tools", []),
                                        "approved_tools": state.get("approved_tools", []),
                                        "candidates": state.get("plan_candidates", []),
                                        "adjustments": state.get("plan_adjustments", [])},
            "actions": state["actions"], "evidence": state["evidence"],
            "timeline": state["timeline"],
            "memory_used": {"recent_messages": len(memory.recent_messages),
                            "history_hits": len(memory.relevant_history),
                            "summary_used": bool(memory.summary), "profile_used": bool(memory.user_profile)},
            "ticket_id": state.get("ticket_id"), "mock": True,
            "decision_mode": decision_mode,
            "prompt_version": PROMPT_VERSION,
            "diagnosis": diagnosis,
            "security": {
                "injection_suspected": bool(security_flags),
                "flags": security_flags,
                "tainted_sources": tainted_sources(security_flags),
                "policy": "仅显式用户请求可授权工具调用；用户文本与工具观察中的指令不会被当作工具指令执行",
            },
            "metrics": {"model": {**metrics, 'latency_ms': round(metrics['latency_ms'], 1)},
                        "tool_calls": actions_this_turn,
                        "tool_latency_ms": round(sum(a.get('latency_ms', 0) for a in state['actions'][-actions_this_turn:]), 1) if actions_this_turn else 0,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)},
        }
        await self.redis.setex(state_key, redis_ttl_seconds(), json.dumps(state, ensure_ascii=False))
        await self.redis.setex(f"developer:run:{run_id}", redis_ttl_seconds(),
                               json.dumps({"user_id": user_id, "response": response}, ensure_ascii=False))
        history_key = 'developer:run-index:' + hashlib.sha256(user_id.encode()).hexdigest()
        await self.redis.zadd(history_key, {run_id: time.time()})
        await self.redis.zremrangebyrank(history_key, 0, -101)
        await self.redis.expire(history_key, redis_ttl_seconds())
        await self.memory.add_message(user_id, conversation_id, MsgRole.USER, message)
        await self.memory.add_message(user_id, conversation_id, MsgRole.ASSISTANT, answer)
        if isinstance(self.model, AnthropicDecisionModel):
            try:
                await asyncio.wait_for(self.memory.update_profile(user_id, conversation_id), timeout=10)
            except (asyncio.TimeoutError, Exception):
                logger.warning('画像更新失败或超时；本轮诊断结果仍可查询')
        return response

    @staticmethod
    def _observation_summary(tool: str, data: Any, error: Optional[str]) -> str:
        if error:
            return f"查询失败：{error[:180]}"
        base = base_tool(tool)
        # An MCP-routed observation says so, so a reader can tell which transport produced it.
        prefix = "MCP 返回" if tool != base else "找到"
        if base in {"search_api_docs", "search_quota_docs"}:
            return f"{prefix} {len(data or [])} 个模拟平台文档片段"
        if base == "search_public_docs":
            return f"{prefix} {len(data or [])} 条公开 API 文档摘要"
        if base == "lookup_error_playbook":
            return f"{prefix} {len(data or [])} 条错误处置条目"
        if base in {"get_request_trace", "get_related_request_trace", "get_webhook_delivery"}:
            return f"HTTP {data.get('status')}；{_untrusted_text(data.get('error_summary'))}"
        if base == "inspect_key_scope":
            return f"状态 {data.get('status')}；scope {', '.join(data.get('scopes', []))}"
        return f"模拟工单 {data.get('ticket_id')} 已保存"
