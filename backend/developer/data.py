"""Versioned, fictional platform data used only by the local demonstration."""

DOCS = [
    {"title": "v1 / 鉴权与权限", "content": "模拟平台 v1 使用 Authorization: Bearer <API Key>。401 表示缺少、失效或格式错误的凭证；403 表示凭证有效但缺少接口所需 scope。请先用 request_id 查询脱敏请求记录，再用 key_prefix 查看状态与 scope。绝不要索取完整密钥。"},
    {"title": "v1 / 速率限制", "content": "模拟平台 v1 的 429 表示当前租户已达到分钟配额。请求记录提供 limit、remaining 与 retry_after_seconds。客户端应等待 Retry-After 后重试，并采用指数退避；不要在限流期间持续快速重试。"},
    {"title": "v1 / Webhook 投递", "content": "模拟平台 v1 会在投递记录中保存 delivery_id、HTTP 状态、重试次数和脱敏错误摘要。401 通常需要核对签名密钥配置；400 应检查请求体与处理器；5xx 应检查接收端可用性。必须先查询 delivery_id，不能仅凭状态码断言根因。"},
    {"title": "v1 / 支持工单", "content": "模拟平台的支持工单只保存演示数据。需要人工处理时先总结已验证证据；只有用户明确要求或确认创建工单，才可调用 create_support_ticket。返回的 ticket_id 仅为本地模拟平台编号。"},
    {"title": "v2 / 鉴权与权限", "content": "模拟平台 v2 使用 X-API-Key 请求头。401 表示凭证缺失或无效；403 表示缺少接口所需权限。请勿提供完整密钥。"},
    {"title": "v2 / 速率限制", "content": "模拟平台 v2 的 429 返回 retry_after_seconds。请等待该时间后使用指数退避重试。"},
]

AUTH_CASES = {}
QUOTA_CASES = {}
WEBHOOK_CASES = {}
KEYS = {}
EVAL_CASES = []

for index in range(1, 11):
    request_id = f"req_auth_{index:02d}"
    key_prefix = f"demo-auth-{index:02d}"
    reason = ("expired_key", "missing_scope", "malformed_header")[(index - 1) % 3]
    status = 403 if reason == "missing_scope" else 401
    AUTH_CASES[request_id] = {
        "request_id": request_id, "api_version": "v1", "endpoint": "/v1/messages",
        "status": status, "reason_code": reason, "key_prefix": key_prefix,
        "error_summary": {
            "expired_key": "凭证已过期", "missing_scope": "缺少 messages:write 权限",
            "malformed_header": "Authorization 头格式不正确",
        }[reason],
    }
    KEYS[key_prefix] = {
        "key_prefix": key_prefix,
        "status": "expired" if reason == "expired_key" else "active",
        "scopes": ["messages:read"] if reason == "missing_scope" else ["messages:write"],
        "api_version": "v1",
    }
    EVAL_CASES.append({"family": "auth", "message": f"请求 {request_id} 返回 {status}，帮我排查", "identifier": request_id, "expected_tools": ["search_api_docs", "get_request_trace", "inspect_key_scope"]})

for index in range(1, 11):
    request_id = f"req_quota_{index:02d}"
    QUOTA_CASES[request_id] = {
        "request_id": request_id, "api_version": "v1", "endpoint": "/v1/messages",
        "status": 429, "reason_code": "minute_quota_exceeded",
        "limit_per_minute": 60 + index * 10, "remaining": 0,
        "retry_after_seconds": 5 + index, "error_summary": "分钟配额已用尽",
    }
    EVAL_CASES.append({"family": "quota", "message": f"请求 {request_id} 遇到 429，如何处理", "identifier": request_id, "expected_tools": ["search_api_docs", "get_request_trace"]})

for index in range(1, 11):
    delivery_id = f"delivery_{index:02d}"
    status = (401, 400, 503)[(index - 1) % 3]
    WEBHOOK_CASES[delivery_id] = {
        "delivery_id": delivery_id, "api_version": "v1", "event_type": "message.created",
        "status": status, "retry_count": index % 4,
        "error_summary": {401: "接收端拒绝签名", 400: "接收端拒绝请求体", 503: "接收端暂不可用"}[status],
    }
    EVAL_CASES.append({"family": "webhook", "message": f"Webhook 投递 {delivery_id} 失败，请排查", "identifier": delivery_id, "expected_tools": ["search_api_docs", "get_webhook_delivery"]})

for index in range(1, 11):
    delivery_id = f'delivery_combo_{index:02d}'
    status = (503, 401, 400)[(index - 1) % 3]
    WEBHOOK_CASES[delivery_id] = {
        'delivery_id': delivery_id, 'api_version': 'v1',
        'event_type': 'message.created', 'status': status, 'retry_count': index % 4,
        'error_summary': {503: '接收端暂不可用', 401: '接收端拒绝签名', 400: '接收端拒绝请求体'}[status],
        'related_request_id': f'req_quota_{index:02d}',
    }

REQUESTS = {**AUTH_CASES, **QUOTA_CASES}
REQUESTS['req_auth_v2_01'] = {
    'request_id': 'req_auth_v2_01', 'api_version': 'v2', 'endpoint': '/v2/messages',
    'status': 401, 'reason_code': 'missing_api_key_header', 'error_summary': '缺少 X-API-Key 请求头',
}
