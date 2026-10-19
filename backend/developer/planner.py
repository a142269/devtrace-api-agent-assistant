"""Evidence requirements, tool dependencies and plan approval."""

from typing import Any, Dict

MCP_PREFIX = "mcp_"


def base_tool(name: str) -> str:
    """Strip the MCP prefix so rules match local and MCP-hosted names alike."""
    text = str(name or "")
    return text[len(MCP_PREFIX):] if text.startswith(MCP_PREFIX) else text


def mcp_name(name: str) -> str:
    return name if str(name).startswith(MCP_PREFIX) else MCP_PREFIX + str(name)


TOOL_TITLES = {
    "search_api_docs": "检索对应版本的 API 文档",
    "search_public_docs": "检索公开 API 文档摘要",
    "lookup_error_playbook": "查询错误处置手册",
    "mcp_search_public_docs": "通过 MCP 检索公开 API 文档",
    "mcp_lookup_error_playbook": "通过 MCP 查询错误处置手册",
    "get_request_trace": "核对脱敏请求记录",
    "inspect_key_scope": "核对密钥状态与权限摘要",
    "get_webhook_delivery": "核对 Webhook 投递记录",
    "get_related_request_trace": "核对投递记录关联的请求",
    "search_quota_docs": "检索限流处理文档",
    "create_support_ticket": "创建模拟支持工单",
}
TOOL_EVIDENCE = {
    "search_api_docs": "匹配故障类型和 API 版本的文档片段",
    "search_public_docs": "公开 API 文档摘要与来源链接",
    "lookup_error_playbook": "错误码、可重试性、退避与恢复步骤",
    "mcp_search_public_docs": "通过 MCP 工具返回的公开文档片段与来源",
    "mcp_lookup_error_playbook": "通过 MCP 工具返回的错误码、可重试性与恢复步骤",
    "get_request_trace": "request_id 对应的脱敏状态和错误摘要",
    "inspect_key_scope": "key_prefix 对应的状态与 scope 摘要",
    "get_webhook_delivery": "delivery_id 对应的状态、重试次数与错误摘要",
    "get_related_request_trace": "投递记录关联 request_id 的脱敏状态和错误摘要",
    "search_quota_docs": "对应版本的限流文档片段",
    "create_support_ticket": "本地模拟工单编号",
}
# JSON Schemas are shared by the runtime tool registry and native model tool calling.
TOOL_SCHEMAS = {
    "search_api_docs": {"type": "object", "properties": {
        "query": {"type": "string"}, "top_k": {"type": "integer"}},
        "required": ["query"], "additionalProperties": False},
    "search_public_docs": {"type": "object", "properties": {
        "query": {"type": "string"}, "api": {"type": "string"}, "top_k": {"type": "integer"}},
        "required": ["query"], "additionalProperties": False},
    "lookup_error_playbook": {"type": "object", "properties": {
        "symptom": {"type": "string"}, "api": {"type": "string"},
        "http_status": {"type": "integer"}, "top_k": {"type": "integer"}},
        "required": ["symptom"], "additionalProperties": False},
    "get_request_trace": {"type": "object", "properties": {
        "request_id": {"type": "string"}}, "required": ["request_id"],
        "additionalProperties": False},
    "inspect_key_scope": {"type": "object", "properties": {
        "key_prefix": {"type": "string"}}, "required": ["key_prefix"],
        "additionalProperties": False},
    "get_webhook_delivery": {"type": "object", "properties": {
        "delivery_id": {"type": "string"}}, "required": ["delivery_id"],
        "additionalProperties": False},
    "get_related_request_trace": {"type": "object", "properties": {
        "request_id": {"type": "string"}}, "required": ["request_id"],
        "additionalProperties": False},
    "search_quota_docs": {"type": "object", "properties": {
        "query": {"type": "string"}, "top_k": {"type": "integer"}},
        "required": ["query"], "additionalProperties": False},
    "create_support_ticket": {"type": "object", "properties": {
        "issue_id": {"type": "string"}}, "required": ["issue_id"],
        "additionalProperties": False},
    "mcp_search_public_docs": {"type": "object", "properties": {
        "query": {"type": "string"}, "api": {"type": "string"}, "top_k": {"type": "integer"}},
        "required": ["query"], "additionalProperties": False},
    "mcp_lookup_error_playbook": {"type": "object", "properties": {
        "symptom": {"type": "string"}, "api": {"type": "string"},
        "http_status": {"type": "integer"}, "top_k": {"type": "integer"}},
        "required": ["symptom"], "additionalProperties": False},
}
KIND_TO_TOOLS = {
    "auth": ["search_api_docs", "get_request_trace", "inspect_key_scope"],
    "quota": ["search_api_docs", "get_request_trace"],
    "webhook": ["search_api_docs", "get_webhook_delivery"],
    "public_api": ["lookup_error_playbook", "search_public_docs"],
}
# When enabled, the public_api path uses the MCP-hosted tools instead of local ones.
PUBLIC_API_MCP_TOOLS = ["mcp_lookup_error_playbook", "mcp_search_public_docs"]
# Kinds whose read tools the agent can route through the MCP server as a whole.
MCP_ROUTED_KINDS = ("auth", "quota", "webhook")
# The MCP server hosts these read tools too, so every planning rule compares base
# names and one plan spelling works for both transports. create_support_ticket is
# deliberately absent: a ticket is a side effect, not a read.
MCP_READ_TOOLS = ("search_api_docs", "search_quota_docs", "get_request_trace",
                  "get_related_request_trace", "inspect_key_scope", "get_webhook_delivery")
for _tool in MCP_READ_TOOLS:
    TOOL_TITLES[mcp_name(_tool)] = f"通过 MCP {TOOL_TITLES[_tool]}"
    TOOL_EVIDENCE[mcp_name(_tool)] = f"通过 MCP 工具返回的{TOOL_EVIDENCE[_tool]}"
    TOOL_SCHEMAS[mcp_name(_tool)] = dict(TOOL_SCHEMAS[_tool])
TOOL_DEPENDENCIES = {"inspect_key_scope": {"get_request_trace"},
                     "get_related_request_trace": {"get_webhook_delivery"},
                     "search_quota_docs": {"get_related_request_trace"}}


def _approved_tools(kind: str, proposed: Any, allowed: list[str] | None = None) -> tuple[list[str], list[str], list[str]]:
    """Keep model ordering where safe, then supply the mandatory evidence chain."""
    raw = [tool for tool in proposed[:10] if isinstance(tool, str)] if isinstance(proposed, list) else []
    allowed = allowed or KIND_TO_TOOLS[kind]
    # A model may name either spelling; the allowed list decides which one runs.
    by_base = {base_tool(tool): tool for tool in allowed}
    chosen = list(dict.fromkeys(by_base[base_tool(tool)] for tool in raw if base_tool(tool) in by_base))
    adjustments = []
    if any(base_tool(tool) not in by_base for tool in raw):
        adjustments.append("移除不属于该故障类型的工具")
    # Require evidence targets, not a fixed set of tools. Scope inspection is
    # conditional on the observed request; it can be added after the trace.
    mandatory = [tool for tool in allowed if base_tool(tool) != 'inspect_key_scope']
    for tool in mandatory:
        if tool not in chosen:
            chosen.append(tool)
            adjustments.append(f"补充必需证据工具 {tool}")
    for base, dependencies in TOOL_DEPENDENCIES.items():
        tool = by_base.get(base)
        if tool is None or tool not in chosen:
            continue
        for dependency_base in dependencies:
            dependency = by_base.get(dependency_base)
            if dependency and dependency in chosen and chosen.index(dependency) > chosen.index(tool):
                chosen.remove(tool)
                chosen.insert(chosen.index(dependency) + 1, tool)
                adjustments.append(f"将 {tool} 移至 {dependency} 之后")
    return raw, chosen, adjustments


def _ready_steps(state: Dict[str, Any]) -> list[Dict[str, Any]]:
    completed = {base_tool(step["tool"]) for step in state["steps"] if step["status"] in {"done", "skipped"}}
    ready = []
    for step in state["steps"]:
        if step["status"] != "pending":
            continue
        tool = base_tool(step["tool"])
        if tool == 'search_api_docs' and state.get('require_version_observation') and 'get_request_trace' not in completed:
            continue
        dependencies = ({base_tool(item['tool']) for item in state['steps'] if item['tool'] and base_tool(item['tool']) != 'create_support_ticket'} if tool == "create_support_ticket"
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


# Candidate planning (optional): score a couple of safe orderings and pick one.
_PLAN_RANK = {
    "get_request_trace": 0, "get_webhook_delivery": 0, "get_related_request_trace": 1,
    "search_quota_docs": 2, "inspect_key_scope": 2, "search_api_docs": 3,
    "search_public_docs": 3, "lookup_error_playbook": 3, "create_support_ticket": 9,
}
# Rank and score by base name so an MCP-routed plan keeps the same ordering.
_DOC_TOOLS = {"search_api_docs", "search_public_docs", "search_quota_docs",
              "lookup_error_playbook"}
_RECORD_TOOLS = {"get_request_trace", "get_webhook_delivery", "get_related_request_trace"}


def _dependency_ok(tools: list[str]) -> bool:
    positions: Dict[str, int] = {}
    for index, name in enumerate(tools):
        positions.setdefault(base_tool(name), index)
    for tool, dependencies in TOOL_DEPENDENCIES.items():
        if tool not in positions:
            continue
        for dependency in dependencies:
            if dependency in positions and positions[dependency] > positions[tool]:
                return False
    return True


def _score_candidate(tools: list[str], allowed: list[str], issue_id: str) -> float:
    coverage = len(set(tools) & set(allowed)) / max(1, len(allowed))
    score = coverage * 4.0
    bases = [base_tool(tool) for tool in tools]
    if issue_id and any(tool in _DOC_TOOLS for tool in bases):
        first_record = min((index for index, tool in enumerate(bases) if tool in _RECORD_TOOLS),
                           default=len(bases))
        first_doc = min((index for index, tool in enumerate(bases) if tool in _DOC_TOOLS),
                        default=len(bases))
        score += 2.0 if first_record < first_doc else 0.0
    if _dependency_ok(tools):
        score += 1.0
    score -= 0.1 * len(tools)
    return round(score, 2)


def build_plan_candidates(kind: str, chosen: list[str], allowed: list[str] | None = None,
                          issue_id: str = "", require_version: bool = False) -> list[dict]:
    """Return safe candidate orderings with scores; never invent tools."""
    allowed = allowed or KIND_TO_TOOLS[kind]
    base = [tool for tool in chosen if tool in allowed]
    if not base:
        base = list(allowed)
    candidates = [{"name": "approved", "tools": base,
                   "score": _score_candidate(base, allowed, issue_id)}]
    evidence_first = sorted(base, key=lambda tool: (_PLAN_RANK.get(base_tool(tool), 5),
                                                    base.index(tool)))
    if evidence_first != base:
        candidates.append({"name": "evidence_first", "tools": evidence_first,
                           "score": _score_candidate(evidence_first, allowed, issue_id)})
    minimal = [tool for tool in allowed if base_tool(tool) != "inspect_key_scope"]
    candidates.append({"name": "minimal", "tools": minimal,
                       "score": _score_candidate(minimal, allowed, issue_id)})
    if require_version:
        candidates = [candidate for candidate in candidates
                      if candidate["name"] != "minimal"
                      or any(base_tool(tool) == "get_request_trace" for tool in candidate["tools"])]
    return candidates


def choose_plan_candidate(candidates: list[dict]) -> list[str]:
    """Highest score wins; ties keep the first (approved) candidate for stability."""
    best = max(candidates, key=lambda item: (item["score"], -candidate_index(candidates, item)))
    return list(best["tools"])


def candidate_index(candidates: list[dict], item: dict) -> int:
    return candidates.index(item)


