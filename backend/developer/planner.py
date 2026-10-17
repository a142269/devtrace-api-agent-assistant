"""Evidence requirements, tool dependencies and plan approval."""

from typing import Any, Dict

TOOL_TITLES = {
    "search_api_docs": "检索对应版本的 API 文档",
    "get_request_trace": "核对脱敏请求记录",
    "inspect_key_scope": "核对密钥状态与权限摘要",
    "get_webhook_delivery": "核对 Webhook 投递记录",
    "get_related_request_trace": "核对投递记录关联的请求",
    "search_quota_docs": "检索限流处理文档",
    "create_support_ticket": "创建模拟支持工单",
}
TOOL_EVIDENCE = {
    "search_api_docs": "匹配故障类型和 API 版本的文档片段",
    "get_request_trace": "request_id 对应的脱敏状态和错误摘要",
    "inspect_key_scope": "key_prefix 对应的状态与 scope 摘要",
    "get_webhook_delivery": "delivery_id 对应的状态、重试次数与错误摘要",
    "get_related_request_trace": "投递记录关联 request_id 的脱敏状态和错误摘要",
    "search_quota_docs": "对应版本的限流文档片段",
    "create_support_ticket": "本地模拟工单编号",
}
KIND_TO_TOOLS = {
    "auth": ["search_api_docs", "get_request_trace", "inspect_key_scope"],
    "quota": ["search_api_docs", "get_request_trace"],
    "webhook": ["search_api_docs", "get_webhook_delivery"],
}
TOOL_DEPENDENCIES = {"inspect_key_scope": {"get_request_trace"},
                     "get_related_request_trace": {"get_webhook_delivery"},
                     "search_quota_docs": {"get_related_request_trace"}}


def _approved_tools(kind: str, proposed: Any) -> tuple[list[str], list[str], list[str]]:
    """Keep model ordering where safe, then supply the mandatory evidence chain."""
    raw = [tool for tool in proposed[:10] if isinstance(tool, str)] if isinstance(proposed, list) else []
    allowed = KIND_TO_TOOLS[kind]
    chosen = list(dict.fromkeys(tool for tool in raw if tool in allowed))
    adjustments = []
    if any(tool not in allowed for tool in raw):
        adjustments.append("移除不属于该故障类型的工具")
    # Require evidence targets, not a fixed set of tools. Scope inspection is
    # conditional on the observed request; it can be added after the trace.
    mandatory = [tool for tool in allowed if tool != 'inspect_key_scope']
    for tool in mandatory:
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
    completed = {step["tool"] for step in state["steps"] if step["status"] in {"done", "skipped"}}
    ready = []
    for step in state["steps"]:
        if step["status"] != "pending":
            continue
        tool = step["tool"]
        if tool == 'search_api_docs' and state.get('require_version_observation') and 'get_request_trace' not in completed:
            continue
        dependencies = ({item['tool'] for item in state['steps'] if item['tool'] and item['tool'] != 'create_support_ticket'} if tool == "create_support_ticket"
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


