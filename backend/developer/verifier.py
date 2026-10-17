"""Construct diagnosis hypotheses only from successful simulated observations."""

CAUSES = {
    'expired_key': '凭证已过期',
    'missing_scope': '缺少接口所需权限',
    'malformed_header': '认证头格式不正确',
    'minute_quota_exceeded': '分钟配额已用尽',
}


def build_diagnosis(kind, actions):
    hypotheses = []
    for action in actions:
        result = action.get('result')
        if not action.get('success') or not isinstance(result, dict):
            continue
        code = result.get('reason_code')
        if code in CAUSES and action.get('tool') == 'get_request_trace':
            hypotheses.append({'code': code, 'description': CAUSES[code],
                               'status': 'supported', 'evidence': [action['source']]})
        elif action.get('tool') == 'get_webhook_delivery':
            # The record reports a failure, but does not necessarily prove its cause.
            hypotheses.append({'code': 'webhook_delivery_failed',
                               'description': str(result.get('error_summary', '投递失败')),
                               'status': 'observed', 'evidence': [action['source']]})
    return {'hypotheses': hypotheses, 'status': 'evidence_available' if hypotheses else 'insufficient_evidence'}
