"""Curated public API documentation summaries used by the public-docs tool.

These are short, attributed summaries of publicly documented behaviour (auth,
rate limits, webhooks, common error codes) from official API documentation.
They are not full vendor documentation and do not contain any credentials.

To add whole specs or own documents, use ``developer.ingest_public_docs`` and
merge the generated JSON into ``PUBLIC_DOCS``.
"""

PUBLIC_DOCS = [
    {
        "id": "github-auth-401",
        "api": "GitHub",
        "title": "GitHub REST API：401 Bad credentials",
        "content": (
            "GitHub REST API 对缺失、无效或过期的 token 返回 401 Bad credentials。"
            "请求应使用 Authorization: Bearer <token>；token 过期、被撤销或权限不足时先核对 token 状态。"
            "401 与 403 不同：403 通常表示已认证但无权访问或触发限流。"
        ),
        "source_url": "https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api",
        "license": "GitHub Docs（CC BY 4.0），仅摘要引用",
        "updated": "2026-10",
        "tags": ["401", "认证", "token"],
    },
    {
        "id": "github-rate-limit",
        "api": "GitHub",
        "title": "GitHub REST API：速率限制与 403/429",
        "content": (
            "GitHub 对已认证请求通常允许每小时 5000 次，未认证请求每小时 60 次。"
            "触发主限流时返回 403 或 429，并在 X-RateLimit-Remaining、X-RateLimit-Reset、Retry-After 头中给出恢复信息；"
            "应按响应头等待后重试，并使用条件请求（ETag/If-None-Match）减少配额消耗。"
        ),
        "source_url": "https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api",
        "license": "GitHub Docs（CC BY 4.0），仅摘要引用",
        "updated": "2026-10",
        "tags": ["429", "403", "限流", "Retry-After"],
    },
    {
        "id": "github-secondary-limit",
        "api": "GitHub",
        "title": "GitHub REST API：二级限流",
        "content": (
            "GitHub 除按小时的主限流外还会对高并发、重复请求施加二级限流，可能返回 403/429 且不带完整的 X-RateLimit 头。"
            "遇到二级限流时应停止突发请求、指数退避、避免并行重复调用同一端点。"
        ),
        "source_url": "https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api",
        "license": "GitHub Docs（CC BY 4.0），仅摘要引用",
        "updated": "2026-10",
        "tags": ["限流", "403", "429", "退避"],
    },
    {
        "id": "github-webhook-delivery",
        "api": "GitHub",
        "title": "GitHub Webhooks：投递失败与重试",
        "content": (
            "GitHub Webhook 投递失败时可在仓库或组织设置的 Recent Deliveries 查看响应码和重试记录；"
            "接收端必须在超时时间内返回 2xx，否则会按策略重试。"
            "排查时应先看投递响应码与重试历史，再核对签名和接收端可用性。"
        ),
        "source_url": "https://docs.github.com/en/webhooks/using-webhooks/handling-webhook-deliveries",
        "license": "GitHub Docs（CC BY 4.0），仅摘要引用",
        "updated": "2026-10",
        "tags": ["Webhook", "投递失败", "重试"],
    },
    {
        "id": "stripe-auth-401",
        "api": "Stripe",
        "title": "Stripe API：401 invalid API Key",
        "content": (
            "Stripe 在 API Key 缺失、格式错误、已被删除或使用了错误的密钥类型时返回 401。"
            "服务端应使用 Secret key，测试与生产密钥不能混用；密钥泄露后应立即在 Dashboard 轮换。"
        ),
        "source_url": "https://docs.stripe.com/error-codes",
        "license": "Stripe 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["401", "认证", "API Key"],
    },
    {
        "id": "stripe-rate-limit",
        "api": "Stripe",
        "title": "Stripe API：429 限流与重试",
        "content": (
            "Stripe 在请求过快时返回 429 Too Many Requests。"
            "应使用指数退避并限制并发，读请求可重试；写请求应配合 Idempotency-Key，避免重试造成重复扣款。"
            "持续 429 应检查是否存在突发流量或轮询替代方案（如 Webhook）。"
        ),
        "source_url": "https://docs.stripe.com/rate-limits",
        "license": "Stripe 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["429", "限流", "退避", "幂等"],
    },
    {
        "id": "stripe-webhook-signature",
        "api": "Stripe",
        "title": "Stripe Webhooks：签名验证失败",
        "content": (
            "Stripe 用 Stripe-Signature 头对 Webhook 载荷签名；签名不匹配、时间戳过期或使用了解析后的 body 都会导致验证失败。"
            "验证必须使用原始请求体与 endpoint 的 signing secret，并允许合理的时钟偏差；失败时应先核对密钥和原始 body。"
        ),
        "source_url": "https://docs.stripe.com/webhooks/signatures",
        "license": "Stripe 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["Webhook", "签名", "400"],
    },
    {
        "id": "stripe-idempotency",
        "api": "Stripe",
        "title": "Stripe API：幂等键与 409",
        "content": (
            "Stripe 支持 Idempotency-Key：相同键的重复请求会返回第一次的结果。"
            "若同一键被用于不同参数，可能返回 409 或错误；重试写请求时应复用原幂等键，而不是生成新键。"
        ),
        "source_url": "https://docs.stripe.com/idempotency",
        "license": "Stripe 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["幂等", "409", "重试"],
    },
    {
        "id": "openai-401",
        "api": "OpenAI",
        "title": "OpenAI API：401 invalid_api_key",
        "content": (
            "OpenAI 对缺失、错误或已撤销的 API Key 返回 401 invalid_api_key。"
            "应核对 Key 是否属于当前组织/项目、是否被撤销，以及请求头是否使用了 Bearer 格式。"
        ),
        "source_url": "https://platform.openai.com/docs/guides/error-codes",
        "license": "OpenAI 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["401", "认证", "API Key"],
    },
    {
        "id": "openai-429",
        "api": "OpenAI",
        "title": "OpenAI API：429 rate_limit_exceeded 与 insufficient_quota",
        "content": (
            "OpenAI 的 429 有两种常见原因：rate_limit_exceeded（请求/Token 速率超限，可退避重试）和 insufficient_quota（额度用尽，重试无效，需要充值或换项目）。"
            "排查时先看错误码与 message，再决定退避还是处理账单。"
        ),
        "source_url": "https://platform.openai.com/docs/guides/error-codes",
        "license": "OpenAI 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["429", "限流", "额度"],
    },
    {
        "id": "twilio-auth-401",
        "api": "Twilio",
        "title": "Twilio API：401 认证失败",
        "content": (
            "Twilio 在 Account SID 或 Auth Token 不正确、请求签名无效时返回 401。"
            "应先核对凭据、环境变量和请求签名；不要在前端或日志中暴露 Auth Token。"
        ),
        "source_url": "https://www.twilio.com/docs/usage/requests-to-twilio",
        "license": "Twilio 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["401", "认证"],
    },
    {
        "id": "twilio-429",
        "api": "Twilio",
        "title": "Twilio API：429 Too Many Requests",
        "content": (
            "Twilio 在超过账户或号码的请求速率时返回 429。"
            "应读取 Retry-After 退避、降低并发并避免短时间重复发送；生产场景应使用队列削峰。"
        ),
        "source_url": "https://www.twilio.com/docs/usage/requests-to-twilio",
        "license": "Twilio 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["429", "限流", "Retry-After"],
    },
    {
        "id": "petstore-404",
        "api": "Petstore",
        "title": "Swagger Petstore：404 Pet not found",
        "content": (
            "OpenAPI 示例 Petstore 对不存在的 petId 返回 404 Pet not found；"
            "路径参数格式错误可能返回 400。排查时先确认资源 ID 与 API 版本/路径前缀是否匹配。"
        ),
        "source_url": "https://petstore.swagger.io/",
        "license": "OpenAPI Initiative 示例（Apache-2.0），仅摘要引用",
        "updated": "2026-10",
        "tags": ["404", "资源不存在", "OpenAPI"],
    },
    {
        "id": "generic-retry-after",
        "api": "通用",
        "title": "HTTP 429/503：Retry-After 与指数退避",
        "content": (
            "HTTP 429（Too Many Requests）和 503（Service Unavailable）常带 Retry-After 头（秒数或 HTTP 日期）。"
            "客户端应优先遵循该值，否则使用指数退避并加随机抖动；非幂等写请求必须配合幂等键，避免重复副作用。"
        ),
        "source_url": "https://developer.mozilla.org/docs/Web/HTTP/Reference/Status/429",
        "license": "MDN（CC BY-SA 2.5），仅摘要引用",
        "updated": "2026-10",
        "tags": ["429", "503", "Retry-After", "退避"],
    },
]

API_ALIASES = {
    "github": "GitHub",
    "stripe": "Stripe",
    "openai": "OpenAI",
    "twilio": "Twilio",
    "petstore": "Petstore",
    "swagger": "Petstore",
}


def detect_public_api(text: str) -> str:
    """Return the canonical API name mentioned in text, or '' when none matches."""
    lowered = text.lower()
    for alias, api in API_ALIASES.items():
        if alias in lowered:
            return api
    return ""
