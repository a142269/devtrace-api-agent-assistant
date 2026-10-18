"""Evidence-driven diagnosis built only from successful simulated observations.

The verifier never asserts a root cause that its own observations do not support.
It also compares observations against each other so that a contradicting record is
surfaced instead of silently picking one side, and records whether a linked
failure is actually explained by its related request.
"""

import re

from developer.security import scan_untrusted

CAUSES = {
    'expired_key': '凭证已过期',
    'missing_scope': '缺少接口所需权限',
    'malformed_header': '认证头格式不正确',
    'missing_api_key_header': '缺少 API Key 请求头',
    'minute_quota_exceeded': '分钟配额已用尽',
}

REMEDIATION = {
    'expired_key': '在模拟平台重新签发凭证后再重试；过期凭证不能继续使用。',
    'missing_scope': '为该密钥补充接口所需 scope 后再重试。',
    'malformed_header': '按对应版本文档修正 Authorization 请求头格式后再重试。',
    'missing_api_key_header': '按 v2 文档改用 X-API-Key 请求头后再重试。',
    'minute_quota_exceeded': '等待 Retry-After 秒后再重试，并采用指数退避。',
    'webhook_delivery_failed': '核验接收端可用性与签名配置；不要仅凭状态码断言根因。',
}

CONFIDENCE = {'supported': 'high', 'contradicted': 'low', 'observed': 'medium'}
RELATED_CAVEAT = '需确认该关联请求的失败是否影响本次投递；关联关系本身不构成因果证据'
KNOWN_KEY_STATUS = {'active', 'expired', 'revoked', 'rotated'}


def _source(action):
    return action.get('source') or action.get('tool')


def _trace_actions(actions):
    return [item for item in actions
            if item.get('success') and isinstance(item.get('result'), dict)
            and item.get('tool') in {'get_request_trace', 'get_related_request_trace'}]


def _first(actions, tool):
    return next((item for item in actions
                 if item.get('success') and isinstance(item.get('result'), dict)
                 and item.get('tool') == tool), None)


def _needed_scope(trace):
    summary = str(trace.get('result', {}).get('error_summary') or '')
    match = re.search(r'缺少\s*([A-Za-z0-9_:.-]+)\s*权限', summary)
    return match.group(1) if match else ''


def _hypothesis(code, source):
    return {
        'code': code,
        'description': CAUSES[code],
        'status': 'supported',
        'confidence': CONFIDENCE['supported'],
        'evidence': [source],
        'contradicted_by': [],
        'next_evidence': None,
        'remediation': REMEDIATION.get(code),
    }


def _cross_check(code, trace, key):
    """Return (verdict, source, explanation) for a trace-level cause against the key record."""
    if key is None:
        return 'unverified', '', ''
    result = key.get('result') or {}
    if code == 'expired_key':
        status = str(result.get('status') or '').strip().lower()
        # Only an explicitly active key contradicts "expired"; revoked/rotated records
        # are not enough evidence to call it a conflict.
        if status == 'active':
            return 'contradicted', _source(key), '请求记录称凭证已过期，但密钥状态记录显示为 active'
        if status == 'expired':
            return 'consistent', _source(key), ''
    if code == 'missing_scope':
        needed = _needed_scope(trace)
        scopes = [str(scope) for scope in (result.get('scopes') or [])]
        if needed and needed in scopes:
            return 'contradicted', _source(key), f'请求记录称缺少 {needed}，但密钥记录已包含该 scope'
        if needed and scopes and needed not in scopes:
            return 'consistent', _source(key), ''
    return 'unverified', '', ''


def _linkage(delivery, related):
    """Describe a delivery/related-request pair without asserting causation."""
    if not delivery or not related:
        return []
    result = related.get('result') or {}
    status = result.get('status')
    delivery_status = (delivery.get('result') or {}).get('status')
    if status == 200:
        return [{
            'delivery_id': (delivery.get('result') or {}).get('delivery_id'),
            'related_request_id': result.get('request_id'),
            'assessment': 'related_request_succeeded',
            'explanation': '关联请求成功，可排除限流或鉴权失败作为本次投递失败的原因',
        }]
    if delivery_status == 503 and status == 429:
        return [{
            'delivery_id': (delivery.get('result') or {}).get('delivery_id'),
            'related_request_id': result.get('request_id'),
            'assessment': 'correlated_unproven',
            'explanation': '两条记录有关联，但不足以证明限流导致投递失败',
        }]
    return [{
        'delivery_id': (delivery.get('result') or {}).get('delivery_id'),
        'related_request_id': result.get('request_id'),
        'assessment': 'related_request_failed_other',
        'explanation': f'关联请求状态为 {status}，与投递失败不能互相解释',
    }]


def _sanitize_description(text):
    if not isinstance(text, str) or not text.strip():
        return '投递失败'
    if scan_untrusted(text):
        return '记录中包含疑似指令内容，已按不可信数据处理'
    return text[:160]


def build_diagnosis(kind, actions):
    """Build ranked hypotheses plus explicit conflicts and linkage notes.

    Only successful observations are read; failed tool calls never create a cause.
    Each hypothesis keeps the legacy ``code``/``description``/``status``/``evidence``
    fields so existing clients and evaluation scripts keep working.
    """
    hypotheses = []
    conflicts = []
    primary = next((item for item in _trace_actions(actions)
                    if item.get('tool') == 'get_request_trace'), None)
    related = next((item for item in _trace_actions(actions)
                    if item.get('tool') == 'get_related_request_trace'), None)
    key = _first(actions, 'inspect_key_scope')
    delivery = _first(actions, 'get_webhook_delivery')

    if primary:
        result = primary.get('result') or {}
        code = result.get('reason_code')
        if isinstance(code, str) and code in CAUSES:
            hypothesis = _hypothesis(code, _source(primary))
            verdict, key_source, explanation = _cross_check(code, primary, key)
            if verdict == 'contradicted':
                hypothesis['status'] = 'contradicted'
                hypothesis['confidence'] = CONFIDENCE['contradicted']
                hypothesis['contradicted_by'] = [key_source]
                hypothesis['next_evidence'] = '需要用户确认密钥是否已轮换，或重新核对请求记录'
                conflicts.append({
                    'code': code,
                    'sources': [_source(primary), key_source],
                    'explanation': explanation,
                })
            elif verdict == 'consistent' and key_source:
                hypothesis['evidence'].append(key_source)
            hypotheses.append(hypothesis)
        elif result.get('retry_after_seconds') is not None:
            hypotheses.append(_hypothesis('minute_quota_exceeded', _source(primary)))
    elif related:
        # A related request can explain nothing about the delivery by itself, so its
        # cause stays an observation to verify instead of a supported root cause.
        result = related.get('result') or {}
        code = result.get('reason_code')
        if isinstance(code, str) and code in CAUSES:
            hypothesis = _hypothesis(code, _source(related))
            hypothesis.update({'status': 'observed', 'confidence': 'low',
                               'scope': 'related_request', 'next_evidence': RELATED_CAVEAT})
            hypotheses.append(hypothesis)
        elif result.get('retry_after_seconds') is not None:
            hypothesis = _hypothesis('minute_quota_exceeded', _source(related))
            hypothesis.update({'status': 'observed', 'confidence': 'low',
                               'scope': 'related_request', 'next_evidence': RELATED_CAVEAT})
            hypotheses.append(hypothesis)

    if delivery:
        result = delivery.get('result') or {}
        summary = _sanitize_description(result.get('error_summary', '投递失败'))
        hypotheses.append({
            'code': 'webhook_delivery_failed',
            'description': summary,
            'status': 'observed',
            'confidence': CONFIDENCE['observed'],
            'evidence': [_source(delivery)],
            'contradicted_by': [],
            'next_evidence': None,
            'remediation': REMEDIATION['webhook_delivery_failed'],
        })

    linkages = _linkage(delivery, related)
    for linkage in linkages:
        if linkage['assessment'] == 'related_request_succeeded':
            for hypothesis in hypotheses:
                if hypothesis['code'] == 'webhook_delivery_failed':
                    hypothesis['next_evidence'] = '已经可以排除关联请求侧原因，需继续核对接收端日志'

    if conflicts:
        status = 'conflicting_evidence'
    elif hypotheses:
        status = 'evidence_available'
    else:
        status = 'insufficient_evidence'

    # Keep the display order identical to the order the evidence was observed.
    order = {item.get('source'): index for index, item in enumerate(actions)}
    hypotheses.sort(key=lambda item: min(order.get(source, 10 ** 6) for source in item['evidence']))

    return {
        'kind': kind,
        'hypotheses': hypotheses,
        'conflicts': conflicts,
        'linkages': linkages,
        'status': status,
        'summary': _summary(hypotheses, conflicts, linkages, status),
    }


def _summary(hypotheses, conflicts, linkages, status):
    if not hypotheses:
        return '没有可核验的观察，暂不推断根因。'
    parts = [f"共 {len(hypotheses)} 条证据假设"]
    supported = sum(1 for item in hypotheses if item['status'] == 'supported')
    if supported:
        parts.append(f"{supported} 条被观察支持")
    if conflicts:
        parts.append('、'.join(conflict['explanation'] for conflict in conflicts))
    for linkage in linkages:
        parts.append(linkage['explanation'])
    if status == 'conflicting_evidence':
        parts.append('证据存在冲突，暂不给出单一根因结论')
    return '；'.join(parts)
