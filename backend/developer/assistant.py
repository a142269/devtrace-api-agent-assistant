"""Plan → action → observation developer assistant, isolated from customer support."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, Optional

import httpx
from anthropic import AsyncAnthropic

from mcp.tool_manager import MCPToolManager, Tool
from memory.conversation_memory import MsgRole

logger = logging.getLogger(__name__)

TOOL_TITLES = {
    "search_api_docs": "检索对应版本的 API 文档",
    "get_request_trace": "核对脱敏请求记录",
    "inspect_key_scope": "核对密钥状态与权限摘要",
    "get_webhook_delivery": "核对 Webhook 投递记录",
    "create_support_ticket": "创建模拟支持工单",
}
TOOL_EVIDENCE = {
    "search_api_docs": "匹配故障类型的 v1 文档片段",
    "get_request_trace": "request_id 对应的脱敏状态和错误摘要",
    "inspect_key_scope": "key_prefix 对应的状态与 scope 摘要",
    "get_webhook_delivery": "delivery_id 对应的状态、重试次数与错误摘要",
    "create_support_ticket": "本地模拟工单编号",
}
KIND_TO_TOOLS = {
    "auth": ["search_api_docs", "get_request_trace", "inspect_key_scope"],
    "quota": ["search_api_docs", "get_request_trace"],
    "webhook": ["search_api_docs", "get_webhook_delivery"],
}
TOOL_DEPENDENCIES = {"inspect_key_scope": {"get_request_trace"}}


def _approved_tools(kind: str, proposed: Any) -> tuple[list[str], list[str], list[str]]:
    """Keep model ordering where safe, then supply the mandatory evidence chain."""
    raw = [tool for tool in proposed[:10] if isinstance(tool, str)] if isinstance(proposed, list) else []
    allowed = KIND_TO_TOOLS[kind]
    chosen = list(dict.fromkeys(tool for tool in raw if tool in allowed))
    adjustments = []
    if any(tool not in allowed for tool in raw):
        adjustments.append("移除不属于该故障类型的工具")
    for tool in allowed:
        if tool not in chosen:
            chosen.append(tool)
            adjustments.append(f"补充必需证据工具 {tool}")
    for tool, dependencies in TOOL_DEPENDENCIES.items():
        if tool in chosen:
            for dependency in dependencies:
                if dependency in chosen and chosen.index(dependency) > chosen.index(tool):
                    chosen.remove(tool)
                    chosen.insert(chosen.index(dependency) + 1, tool)
                    adjustments.append(f"将 {tool} 移至 {dependency} 之后")
    return raw, chosen, adjustments


def _ready_steps(state: Dict[str, Any]) -> list[Dict[str, Any]]:
    completed = {step["tool"] for step in state["steps"] if step["status"] == "done"}
    ready = []
    for step in state["steps"]:
        if step["status"] != "pending":
            continue
        tool = step["tool"]
        dependencies = (set(KIND_TO_TOOLS[state["kind"]]) if tool == "create_support_ticket"
                        else TOOL_DEPENDENCIES.get(tool, set()))
        if not dependencies.issubset(completed):
            continue
        if tool in {"get_request_trace", "get_webhook_delivery", "create_support_ticket"} and not state.get("issue_id"):
            continue
        if tool == "inspect_key_scope" and not state.get("key_prefix"):
            continue
        if tool == "create_support_ticket" and not state.get("ticket_authorized"):
            continue
        ready.append(step)
    return ready


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
    if "delivery_" in text or "webhook" in text or "回调" in text or "投递" in text:
        return "webhook"
    if "quota" in text or "429" in text or "限流" in text or "配额" in text:
        return "quota"
    return "auth"


class RuleDecisionModel:
    """Deterministic reference model for offline tests and optional local demo."""

    async def make_plan(self, message: str, memory: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        kind = previous.get("kind") if previous and previous.get("status") == "waiting" else _kind(message)
        return {"kind": kind, "goal": f"排查{kind}接入问题", "tools": KIND_TO_TOOLS[kind]}

    async def choose_action(self, state: Dict[str, Any], message: str) -> Dict[str, Any]:
        pending = next(iter(state.get("ready_steps", _ready_steps(state))), None)
        if not pending:
            return {"action": "finish"}
        tool = pending["tool"]
        issue_id = state.get("issue_id", "")
        args = {
            "search_api_docs": {"query": f"v1 {state['kind']} {message[:100]}", "top_k": 3},
            "get_request_trace": {"request_id": issue_id},
            "inspect_key_scope": {"key_prefix": state.get("key_prefix", "")},
            "get_webhook_delivery": {"delivery_id": issue_id},
            "create_support_ticket": {"issue_id": issue_id},
        }[tool]
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
            if item["tool"] == "search_api_docs" and isinstance(data, list):
                titles = [str(doc.get("title")) for doc in data if isinstance(doc, dict) and doc.get("title")]
                if titles:
                    lines.append("已核对模拟平台文档：" + "、".join(titles) + "。")
            elif item["tool"] == "get_request_trace" and isinstance(data, dict):
                lines.append(f"请求 {data.get('request_id')} 返回 {data.get('status')}：{data.get('error_summary')}。")
                if data.get("retry_after_seconds") is not None:
                    lines.append(f"请等待 Retry-After {data['retry_after_seconds']} 秒后再重试。")
            elif item["tool"] == "inspect_key_scope" and isinstance(data, dict):
                lines.append(f"密钥前缀 {data.get('key_prefix')} 的状态为 {data.get('status')}，scope 为 {', '.join(data.get('scopes', []))}。")
            elif item["tool"] == "get_webhook_delivery" and isinstance(data, dict):
                lines.append(f"投递 {data.get('delivery_id')} 返回 {data.get('status')}：{data.get('error_summary')}。")
            elif item["tool"] == "create_support_ticket" and isinstance(data, dict):
                lines.append(f"已创建模拟工单 {data.get('ticket_id')}。")
        return "\n".join(lines) + "\n以上仅基于本地模拟平台数据；如需排查真实系统，请提供真实平台可用的脱敏记录。"


class AnthropicDecisionModel(RuleDecisionModel):
    """Model-backed planning and action selection with a safe deterministic fallback."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None, skill: str = ""):
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncAnthropic(**kwargs)
        self.model = model
        self.skill = skill[:4000]

    async def _ask(self, prompt: str) -> Dict[str, Any]:
        response = await self.client.messages.create(
            model=self.model, max_tokens=4096, temperature=0,
            system="你是开发者平台接入诊断 Agent。只输出 JSON，不暴露内部推理。" + self.skill,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            return _json_text(response.content)
        except ValueError:
            logger.warning("模型响应缺少有效 JSON: stop_reason=%s, text_lengths=%s",
                           response.stop_reason,
                           [len(getattr(block, "text", None) or "") for block in response.content])
            raise

    async def make_plan(self, message: str, memory: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fallback = await super().make_plan(message, memory, previous)
        try:
            result = await self._ask(
                "根据问题生成可执行的多步计划。只允许 kind=auth/quota/webhook；对应工具分别是 "
                "auth:[search_api_docs,get_request_trace,inspect_key_scope], "
                "quota:[search_api_docs,get_request_trace], webhook:[search_api_docs,get_webhook_delivery]。"
                "tools 必须覆盖该故障类型的全部取证工具，可根据已有标识和问题线索调整顺序；"
                "鉴权权限摘要必须在请求记录之后。"
                "返回 {\"kind\":...,\"goal\":...,\"tools\":[...]}。"
                f"\n用户消息：{message[:1000]}\n历史摘要：{memory[:1200]}"
            )
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
            result = await self._ask(
                "按当前计划和已有观察选择下一项行动。只允许从 ready_steps 选择步骤及其绑定的工具；"
                "不要伪造标识符或工具结果。返回 {\"action\":\"tool\",\"step_id\":...,\"tool\":...,\"args\":{...}}。"
                f"\n计划与观察：{json.dumps(state, ensure_ascii=False, default=str)[:5000]}"
                f"\n当前消息：{message[:500]}"
            )
            ready = state.get("ready_steps", _ready_steps(state))
            selected = next((step for step in ready if step["id"] == result.get("step_id")
                             and step["tool"] == result.get("tool")), None)
            if (selected and result.get("action") == "tool" and isinstance(result.get("args"), dict)):
                # Tool arguments are still checked by the manager and the executor.
                return result
        except Exception as ex:
            logger.warning("开发者行动选择失败，按计划执行 (HTTP %s)", getattr(ex, "status_code", "unknown"))
        return {**fallback, "_fallback": True}

    async def revise_plan(self, state: Dict[str, Any], failed_tool: str) -> Dict[str, Any]:
        try:
            result = await self._ask(
                "工具失败后给出下一步。不能重复查询同一个失败标识符。"
                "返回 {\"ask_user\":\"一句明确的补充信息请求\"}。"
                f"\n失败工具：{failed_tool}\n状态：{json.dumps(state, ensure_ascii=False, default=str)[:4000]}"
            )
            if isinstance(result.get("ask_user"), str) and result["ask_user"].strip():
                return {"ask_user": result["ask_user"].strip()[:240]}
        except Exception:
            pass
        return {**(await super().revise_plan(state, failed_tool)), "_fallback": True}


class DeveloperAssistant:
    MAX_ACTIONS = 5

    def __init__(self, *, memory: Any, knowledge: Any, redis_client: Any, model: Any,
                 tool_manager: MCPToolManager, sandbox_url: str):
        self.memory = memory
        self.knowledge = knowledge
        self.redis = redis_client
        self.model = model
        self.tools = tool_manager
        self.sandbox_url = sandbox_url.rstrip("/")
        self._register_tools()

    def _register_tools(self) -> None:
        async def docs(params, _context):
            query = params["query"]
            results = await self.knowledge.search_async(query, top_k=4)
            kind_match = re.match(r"^v1\s+(auth|quota|webhook)\b", query, re.I)
            titles = {"auth": "鉴权", "quota": "速率限制", "webhook": "Webhook"}
            if not kind_match:
                return results[:params.get("top_k", 3)]
            marker = titles[kind_match.group(1).lower()]
            return [item for item in results if marker.lower() in str(item.get("title", "")).lower()]

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
            ("create_support_ticket", ticket, {"issue_id": "string"}, ["issue_id"], 0),
        ]
        for name, handler, props, required, ttl in definitions:
            self.tools.register(Tool(
                name=name, description=TOOL_TITLES[name], handler=handler,
                schema={"type": "object", "properties": {key: {"type": value} for key, value in props.items()},
                        "required": required, "additionalProperties": False},
                cache_ttl=ttl, timeout_s=12,
            ))

    @staticmethod
    def _state_key(user_id: str, conversation_id: str) -> str:
        digest = hashlib.sha256(f"{user_id}\0{conversation_id}".encode()).hexdigest()
        return f"developer:plan:{digest}"

    async def run(self, message: str, user_id: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        message = _redact(message)
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
            proposed, chosen, adjustments = _approved_tools(kind, plan.get("tools"))
            if plan.get("kind") != kind:
                adjustments.append("根据用户提供的问题标识修正故障类型")
            state = {
                "kind": kind, "goal": str(plan.get("goal") or f"排查 {kind} 接入问题")[:160],
                "proposed_tools": proposed, "approved_tools": chosen,
                "plan_adjustments": adjustments,
                "issue_id": issue_id, "key_prefix": "", "status": "running",
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
        ticket_cancelled = bool(TICKET_CANCEL_RE.search(message))
        ticket_requested_now = bool(TICKET_RE.search(message)) and not ticket_cancelled
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
        while actions_this_turn < self.MAX_ACTIONS:
            pending_steps = [step for step in state["steps"] if step["status"] == "pending"]
            if not pending_steps:
                state["status"] = "complete"
                break
            ready = _ready_steps(state)
            if not ready:
                blocked = pending_steps[0]
                blocked["status"] = "waiting"
                if blocked["tool"] == "create_support_ticket" and not ticket_requested:
                    question = "如需创建模拟支持工单，请明确回复“创建工单”。"
                elif not state["issue_id"]:
                    question = "请提供模拟平台的 request_id（如 req_auth_01）或 delivery_id（如 delivery_01）。"
                elif blocked["tool"] == "inspect_key_scope" and not state.get("key_prefix"):
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
            state["timeline"].append({"type": "selection", "text": f"选择 {pending['id']}：{TOOL_TITLES[tool]}", "step_id": pending["id"]})
            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            # Identifiers come from observed records or user input, never solely from the model.
            if tool == "get_request_trace":
                args = {"request_id": state["issue_id"]}
            elif tool == "get_webhook_delivery":
                args = {"delivery_id": state["issue_id"]}
            elif tool == "inspect_key_scope":
                args = {"key_prefix": state["key_prefix"]}
            elif tool == "create_support_ticket":
                args = {"issue_id": state["issue_id"]}
            elif tool == "search_api_docs":
                args = {"query": f"v1 {state['kind']} {message[:120]}", "top_k": 3}
            context = {
                "user_id": user_id, "conversation_id": conversation_id,
                "idempotency_key": hashlib.sha256(f"{user_id}\0{conversation_id}\0{state['issue_id']}".encode()).hexdigest(),
                "summary": f"{state['goal']}；已核验来源：{', '.join(state['evidence'])}"[:1000],
            }
            result = await self.tools.call(tool, args, context=context, use_cache=(tool == "search_api_docs"))
            actions_this_turn += 1
            data = result.data if result.success else None
            source = f"{tool}:{args.get('request_id') or args.get('delivery_id') or args.get('key_prefix') or 'v1'}"
            action = {"step_id": pending["id"], "tool": tool, "input": args, "success": bool(result.success and data),
                      "summary": self._observation_summary(tool, data, result.error), "source": source,
                      "result": data, "error": result.error or "", "latency_ms": round(result.latency_ms, 1)}
            state["actions"].append(action)
            state["timeline"].append({"type": "action", "text": TOOL_TITLES[tool], "step_id": pending["id"]})
            state["timeline"].append({"type": "observation", "text": action["summary"], "step_id": pending["id"]})
            pending["status"] = "done" if result.success and data else "failed"
            state["timeline"].append({"type": "status", "text": f"{pending['id']} → {pending['status']}", "step_id": pending["id"]})
            if pending["status"] == "done":
                state["evidence"].append(source)
                if tool == "search_api_docs":
                    state["evidence"].extend(
                        f"doc:{doc['title']}" for doc in data
                        if isinstance(doc, dict) and doc.get("title")
                    )
                if tool == "get_request_trace" and isinstance(data, dict):
                    state["key_prefix"] = data.get("key_prefix", "")
                if tool == "create_support_ticket" and isinstance(data, dict):
                    state["ticket_id"] = data.get("ticket_id")
            else:
                if state["replans"] < 1:
                    state["replans"] += 1
                    if state.get("model_fallback") and isinstance(self.model, AnthropicDecisionModel):
                        revision = await RuleDecisionModel.revise_plan(self.model, state, tool)
                    else:
                        revision = await self.model.revise_plan(state, tool)
                    if revision.get("_fallback"):
                        state["model_fallback"] = True
                    question = str(revision.get("ask_user") or "请核对问题标识后重试。")[:240]
                    state["steps"].append({
                        "id": f"s{len(state['steps'])+1}", "title": "补充标识并恢复计划",
                        "tool": None, "required_evidence": "用户提供可查询的问题标识", "status": "waiting",
                    })
                    state["timeline"].append({"type": "revision", "text": "计划调整：等待用户补充正确标识"})
                else:
                    question = "工具仍不可用，请核对标识或稍后重试。"
                state["status"] = "waiting"
                break
        if state["status"] == "running":
            if any(step["status"] == "pending" for step in state["steps"]):
                state["status"] = "waiting"
                question = "已达到本轮工具调用上限，请发送“继续”完成余下步骤。"
            else:
                state["status"] = "complete"
        answer = await self.model.summarize(state, memory_text)
        if question:
            answer = answer + "\n" + question
        if "模拟平台" not in answer:
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
                                        "adjustments": state.get("plan_adjustments", [])},
            "actions": state["actions"], "evidence": state["evidence"],
            "timeline": state["timeline"],
            "memory_used": {"recent_messages": len(memory.recent_messages),
                            "history_hits": len(memory.relevant_history),
                            "summary_used": bool(memory.summary), "profile_used": bool(memory.user_profile)},
            "ticket_id": state.get("ticket_id"), "mock": True,
            "decision_mode": decision_mode,
        }
        await self.redis.setex(state_key, 86400, json.dumps(state, ensure_ascii=False))
        await self.redis.setex(f"developer:run:{run_id}", 86400,
                               json.dumps({"user_id": user_id, "response": response}, ensure_ascii=False))
        await self.memory.add_message(user_id, conversation_id, MsgRole.USER, message)
        await self.memory.add_message(user_id, conversation_id, MsgRole.ASSISTANT, answer)
        if isinstance(self.model, AnthropicDecisionModel):
            asyncio.create_task(self.memory.update_profile(user_id, conversation_id))
        return response

    @staticmethod
    def _observation_summary(tool: str, data: Any, error: Optional[str]) -> str:
        if error:
            return f"查询失败：{error[:180]}"
        if tool == "search_api_docs":
            return f"找到 {len(data or [])} 个模拟平台文档片段"
        if tool == "get_request_trace":
            return f"HTTP {data.get('status')}；{data.get('error_summary')}"
        if tool == "inspect_key_scope":
            return f"状态 {data.get('status')}；scope {', '.join(data.get('scopes', []))}"
        if tool == "get_webhook_delivery":
            return f"HTTP {data.get('status')}；{data.get('error_summary')}"
        return f"模拟工单 {data.get('ticket_id')} 已保存"
