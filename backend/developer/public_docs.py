"""Curated public API documentation summaries used by the public-docs tool.

These are short, attributed summaries of publicly documented behaviour (auth,
rate limits, webhooks, common error codes) from official API documentation.
They are not full vendor documentation and do not contain any credentials.

To add whole specs or own documents, use ``developer.ingest_public_docs`` and put
the generated JSON under ``public_docs_data/``; it is loaded automatically below.

The imported Stripe operations come from https://github.com/stripe/openapi (MIT).
"""

from __future__ import annotations

import json
from pathlib import Path

PUBLIC_DOCS_DIR = Path(__file__).resolve().parent / "public_docs_data"

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
    {
        "id": "deepseek-auth-401",
        "api": "DeepSeek",
        "title": "DeepSeek API：401 Authentication Fails",
        "content": (
            "DeepSeek API 在 API Key 错误或缺失时返回 401 Authentication Fails；"
            "请求使用 Authorization: Bearer <DeepSeek API Key>，OpenAI SDK 可用 base_url=https://api.deepseek.com。"
            "DeepSeek 同时提供 Anthropic 兼容入口，DeepSeek Harness/Claude Code 等场景可按官方文档配置。"
        ),
        "source_url": "https://api-docs.deepseek.com/quick_start/error_codes",
        "license": "DeepSeek API Docs，仅摘要引用",
        "updated": "2026-10",
        "tags": ["401", "认证", "OpenAI 兼容", "Anthropic 兼容"],
    },
    {
        "id": "deepseek-balance-402",
        "api": "DeepSeek",
        "title": "DeepSeek API：402 Insufficient Balance",
        "content": (
            "DeepSeek 在账户余额不足时返回 402 Insufficient Balance。"
            "这是计费问题，重试不会成功；应检查账户余额并充值后再请求，"
            "与 429 限流（请求过快/并发超限）要区分处理。"
        ),
        "source_url": "https://api-docs.deepseek.com/quick_start/error_codes",
        "license": "DeepSeek API Docs，仅摘要引用",
        "updated": "2026-10",
        "tags": ["402", "余额", "计费"],
    },
    {
        "id": "deepseek-rate-limit",
        "api": "DeepSeek",
        "title": "DeepSeek API：429 并发/限流隔离",
        "content": (
            "DeepSeek 按账号计算并发限制（官方文档当前给出 deepseek-flash 2500、deepseek-v4-pro 500），"
            "超过并发或 QPS 限制时返回 429 Rate Limit Reached。可传 user_id 做内容安全、KVCache 与调度的用户级隔离，"
            "user_id 需匹配 [a-zA-Z0-9-_]+ 且不超过 512 字符。遇到 429 应降低并发、按请求节奏退避。"
        ),
        "source_url": "https://api-docs.deepseek.com/quick_start/rate_limit",
        "license": "DeepSeek API Docs，仅摘要引用",
        "updated": "2026-10",
        "tags": ["429", "并发", "限流", "user_id"],
    },
    {
        "id": "deepseek-invalid-params",
        "api": "DeepSeek",
        "title": "DeepSeek API：400/422 请求格式与参数错误",
        "content": (
            "DeepSeek 对请求体格式错误返回 400 Invalid Format，对参数非法返回 422 Invalid Parameters；"
            "应按错误提示修正请求体或参数，例如模型名、消息结构、max_tokens 等。"
            "这两类错误通常重试无效，应先修正请求。"
        ),
        "source_url": "https://api-docs.deepseek.com/quick_start/error_codes",
        "license": "DeepSeek API Docs，仅摘要引用",
        "updated": "2026-10",
        "tags": ["400", "422", "参数错误"],
    },
    {
        "id": "deepseek-server-keepalive",
        "api": "DeepSeek",
        "title": "DeepSeek API：500/503 与长请求 keep-alive",
        "content": (
            "DeepSeek 在服务端异常时返回 500 Server Error，高负载时返回 503 Server Overloaded，应短暂等待后退避重试。"
            "长请求等待期间，非流式响应可能持续返回空行，流式响应会发送 SSE 注释 `: keep-alive`，"
            "解析时要忽略它们；若 10 分钟内未开始推理，服务端会关闭连接。"
        ),
        "source_url": "https://api-docs.deepseek.com/quick_start/rate_limit",
        "license": "DeepSeek API Docs，仅摘要引用",
        "updated": "2026-10",
        "tags": ["500", "503", "keep-alive", "SSE"],
    },
    {
        "id": "anthropic-auth",
        "api": "Anthropic",
        "title": "Anthropic API：401/403 认证与权限",
        "content": (
            "Anthropic Messages API 使用 x-api-key 与 anthropic-version 请求头；"
            "401 authentication_error 表示 API Key 无效或缺失，403 permission_error 表示已认证但无权访问该资源/模型。"
            "先核对 Key、版本头和工作区权限，不要用 401 的思路处理 403。"
        ),
        "source_url": "https://docs.anthropic.com/en/api/errors",
        "license": "Anthropic 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["401", "403", "认证", "x-api-key"],
    },
    {
        "id": "anthropic-rate-limit",
        "api": "Anthropic",
        "title": "Anthropic API：429 限流与 529 过载",
        "content": (
            "Anthropic 在请求超过速率限制时返回 429 rate_limit_error，响应包含 anthropic-ratelimit-* 头（如剩余请求/Token、重置时间）；"
            "服务过载时返回 529 overloaded_error。应读取 Retry-After 或 ratelimit 重置时间，使用指数退避并降低并发，避免持续重试。"
        ),
        "source_url": "https://docs.anthropic.com/en/api/rate-limits",
        "license": "Anthropic 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["429", "529", "限流", "Retry-After"],
    },
    {
        "id": "anthropic-request-errors",
        "api": "Anthropic",
        "title": "Anthropic API：400/413/404/500 错误",
        "content": (
            "Anthropic 的 400 invalid_request_error 表示请求参数非法（如缺少 max_tokens、消息格式错误），"
            "413 request_too_large 表示请求体过大，404 not_found_error 表示资源不存在，500 api_error 表示服务端错误。"
            "4xx 通常应修正请求而不是重试；500/529 才适合退避重试。"
        ),
        "source_url": "https://docs.anthropic.com/en/api/errors",
        "license": "Anthropic 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["400", "413", "404", "500", "错误码"],
    },
    {
        "id": "anthropic-streaming",
        "api": "Anthropic",
        "title": "Anthropic API：Messages 流式事件",
        "content": (
            "Anthropic Messages API 的流式响应使用 SSE，主要事件包括 message_start、content_block_start、"
            "content_block_delta（text_delta / input_json_delta）、content_block_stop、message_delta 与 message_stop；"
            "解析时按事件类型累积文本和工具调用 JSON，不能把每个 data 行都当作完整消息。"
        ),
        "source_url": "https://docs.anthropic.com/en/api/messages-streaming",
        "license": "Anthropic 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["SSE", "流式", "Messages API"],
    },
    {
        "id": "openai-context-length",
        "api": "OpenAI",
        "title": "OpenAI API：400 context_length_exceeded",
        "content": (
            "OpenAI 在输入加输出超过模型上下文窗口时返回 400 context_length_exceeded。"
            "应裁剪历史消息、减少 max_tokens 或改用更大上下文模型；"
            "原样重试同一请求不会成功。"
        ),
        "source_url": "https://platform.openai.com/docs/guides/error-codes",
        "license": "OpenAI 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["400", "上下文长度", "max_tokens"],
    },
    {
        "id": "openai-server-errors",
        "api": "OpenAI",
        "title": "OpenAI API：500/503 服务端错误",
        "content": (
            "OpenAI 在服务端异常时返回 500，容量不足时返回 503。"
            "应读取 Retry-After 并使用指数退避和随机抖动重试，避免雪崩；"
            "持续失败时检查状态页和请求频率，而不是继续快速重试。"
        ),
        "source_url": "https://platform.openai.com/docs/guides/error-codes",
        "license": "OpenAI 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["500", "503", "退避"],
    },
    {
        "id": "gemini-auth",
        "api": "Gemini",
        "title": "Gemini API：API Key 无效与权限",
        "content": (
            "Gemini API 通过 x-goog-api-key 头（或 key 查询参数）传 API Key；"
            "Key 无效时常见 400 API_KEY_INVALID，已认证但无权访问项目/模型时返回 403 PERMISSION_DENIED。"
            "应核对 Key 所属项目、API 是否启用以及模型名。"
        ),
        "source_url": "https://ai.google.dev/gemini-api/docs/troubleshooting",
        "license": "Google 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["400", "401", "403", "API Key"],
    },
    {
        "id": "gemini-429",
        "api": "Gemini",
        "title": "Gemini API：429 RESOURCE_EXHAUSTED",
        "content": (
            "Gemini API 在超过项目/模型配额时返回 429 RESOURCE_EXHAUSTED，错误详情可能带 retryDelay；"
            "应按 retryDelay 或指数退避重试，并检查配额是按分钟/天还是按模型限制。"
        ),
        "source_url": "https://ai.google.dev/gemini-api/docs/troubleshooting",
        "license": "Google 官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["429", "配额", "retryDelay"],
    },
    {
        "id": "qwen-auth",
        "api": "Qwen",
        "title": "通义千问/DashScope：401 InvalidApiKey",
        "content": (
            "DashScope（通义千问）在 API Key 错误或缺失时返回 401 InvalidApiKey；"
            "OpenAI 兼容模式可用 base_url=https://dashscope.aliyuncs.com/compatible-mode/v1。"
            "应核对 Key 所属地域与业务空间，不要混用其他厂商的 Key。"
        ),
        "source_url": "https://help.aliyun.com/zh/model-studio/developer-reference/error-code",
        "license": "阿里云官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["401", "认证", "OpenAI 兼容"],
    },
    {
        "id": "qwen-429",
        "api": "Qwen",
        "title": "通义千问/DashScope：429 限流",
        "content": (
            "DashScope 在超过 QPS/并发或配额时返回 429，错误码常见 Throttling.RateQuota、Throttling.AllocationQuota 等；"
            "应降低并发、按配额退避重试，并区分限流与免费额度耗尽。"
        ),
        "source_url": "https://help.aliyun.com/zh/model-studio/developer-reference/error-code",
        "license": "阿里云官方公开文档，仅摘要引用",
        "updated": "2026-10",
        "tags": ["429", "限流", "配额"],
    },
]


def _load_imported_docs():
    """Load additional documents generated by developer.ingest_public_docs."""
    documents = []
    if not PUBLIC_DOCS_DIR.is_dir():
        return documents
    for path in sorted(PUBLIC_DOCS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            documents.extend(doc for doc in data if isinstance(doc, dict) and doc.get("id"))
    return documents


_seen_ids = {doc["id"] for doc in PUBLIC_DOCS}
PUBLIC_DOCS.extend(doc for doc in _load_imported_docs() if doc["id"] not in _seen_ids)

API_ALIASES = {
    "github": "GitHub",
    "stripe": "Stripe",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "anthropic": "Anthropic",
    "claude": "Anthropic",
    "gemini": "Gemini",
    "qwen": "Qwen",
    "dashscope": "Qwen",
    "通义": "Qwen",
    "tongyi": "Qwen",
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
