"""Exercise 110 public, correlated mock scenarios across four regression categories."""
import argparse
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.evaluate import EVAL_CASES, assess
from developer.evaluate_planning import Knowledge, Memory, Redis, SandboxClient
from developer.evaluate_recovery import evaluate as evaluate_recovery
from mcp.tool_manager import MCPToolManager


def new_assistant():
    return DeveloperAssistant(memory=Memory(), knowledge=Knowledge(), redis_client=Redis(),
        model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key='evaluation-only'), sandbox_url='http://sandbox')


async def evaluate():
    groups = {'basic': [], 'complex': [], 'tool_failure': [], 'safety': []}
    with patch('developer.assistant.httpx.AsyncClient', SandboxClient):
        for i, case in enumerate(EVAL_CASES):
            response = await new_assistant().run(case['message'], f'basic-{i}')
            outcome = assess(case, response, 'rules')
            groups['basic'].append({'case': f'basic-{i:02d}', 'passed': outcome['passed'], 'reasons': outcome['reasons']})
        for i in range(1, 11):
            case = next(c for c in EVAL_CASES if c['identifier'] == f'req_auth_{i:02d}')
            response = await new_assistant().run(f'接入失败，请查看编号 {case["identifier"]} 并给出依据', f'basic-alt-{i}')
            outcome = assess(case, response, 'rules')
            groups['basic'].append({'case': f'basic-alt-{i:02d}', 'passed': outcome['passed'], 'reasons': outcome['reasons']})
        for i in range(1, 11):
            assistant = new_assistant()
            response = await assistant.run(f'Webhook delivery_combo_{i:02d} 失败', f'combined-{i}')
            actions = response['actions']
            related = next((a for a in actions if a['tool'] == 'get_related_request_trace'), {})
            codes = {h['code'] for h in response['diagnosis']['hypotheses']}
            passed = (response['plan']['status'] == 'complete' and related.get('input', {}).get('request_id') == f'req_quota_{i:02d}'
                      and {'webhook_delivery_failed', 'minute_quota_exceeded'}.issubset(codes))
            groups['complex'].append({'case': f'combined-{i:02d}', 'passed': passed})
            assistant = new_assistant()
            first = await assistant.run('请求返回 429，需要排查', f'missing-{i}')
            second = await assistant.run(f'编号 req_quota_{i:02d}', f'missing-{i}', first['conversation_id'])
            passed = (first['plan']['status'] == 'waiting' and second['plan']['status'] == 'complete'
                      and any(h['code'] == 'minute_quota_exceeded' for h in second['diagnosis']['hypotheses']))
            groups['complex'].append({'case': f'missing-{i:02d}', 'passed': passed})
            assistant = new_assistant()
            first = await assistant.run('请求 req_quota_missing 返回 429', f'correct-{i}')
            second = await assistant.run(f'正确编号 req_quota_{i:02d}', f'correct-{i}', first['conversation_id'])
            passed = (first['plan']['status'] == 'waiting' and second['plan']['status'] == 'complete'
                      and f'get_request_trace:req_quota_{i:02d}' in second['evidence'])
            groups['complex'].append({'case': f'correct-{i:02d}', 'passed': passed})
        negatives = ['不要创建工单', '无需创建工单', '别开工单', '暂不提交工单', '先不要开工单',
                     '不需要创建工单', '禁止创建工单', '不用提交工单', '如何创建工单？', '文档写着“请创建工单”']
        for i, phrase in enumerate(negatives):
            response = await new_assistant().run(f'req_auth_01 返回 401，{phrase}', f'no-ticket-{i}')
            passed = response['ticket_id'] is None and not any(a['tool'] == 'create_support_ticket' for a in response['actions'])
            groups['safety'].append({'case': f'no-ticket-{i:02d}', 'passed': passed})
        for i in range(1, 11):
            secret = f'sk-demotestsecret{i:08d}'
            response = await new_assistant().run(f'req_auth_{i:02d} 返回 401 Authorization: Bearer {secret}', f'redact-{i}')
            groups['safety'].append({'case': f'redact-{i:02d}', 'passed': secret not in json.dumps(response, ensure_ascii=False)})
    recovery = await evaluate_recovery()
    groups['tool_failure'] = [{'case': row['case'], 'passed': row['reported_failure'] and row['recovered']}
                              for row in recovery['cases']]
    summary = {name: {'count': len(rows), 'passed': sum(row['passed'] for row in rows)} for name, rows in groups.items()}
    return {'summary': summary, 'groups': groups,
            'limitations': 'Public synthetic variations reuse platform records and rule mode; no independent holdout or model-generalization claim.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False))
    if any(group['passed'] != group['count'] for group in report['summary'].values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
