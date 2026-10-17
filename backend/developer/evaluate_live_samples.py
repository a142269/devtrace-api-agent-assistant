"""Run four labelled sample diagnoses with the configured model against the mock platform."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
from developer.assistant import AnthropicDecisionModel, DeveloperAssistant
from developer.evaluate import EVAL_CASES, assess
from developer.evaluate_planning import Knowledge, Memory, Redis, SandboxClient
from mcp.tool_manager import MCPToolManager


async def evaluate():
    key = os.getenv('ANTHROPIC_API_KEY', '')
    if not key:
        raise RuntimeError('ANTHROPIC_API_KEY is not set')
    model = os.getenv('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')
    base_url = os.getenv('ANTHROPIC_BASE_URL') or None
    assistant = DeveloperAssistant(memory=Memory(), knowledge=Knowledge(), redis_client=Redis(),
        model=AnthropicDecisionModel(key, model, base_url),
        tool_manager=MCPToolManager(api_key=key, base_url=base_url, model=model), sandbox_url='http://sandbox')
    cases = [next(item for item in EVAL_CASES if item['identifier'] == ident)
             for ident in ('req_auth_01', 'req_quota_01', 'delivery_01')]
    cases.append({'identifier': 'delivery_combo_01', 'family': 'combined',
                  'message': 'Webhook delivery_combo_01 失败，请排查关联问题',
                  'expected_cause': 'minute_quota_exceeded'})
    rows = []
    with patch('developer.assistant.httpx.AsyncClient', SandboxClient):
        for case in cases:
            response = await assistant.run(case['message'], 'live-eval')
            row = assess(case, response, 'model')
            row['metrics'] = response['metrics']
            row['hypotheses'] = response['diagnosis']['hypotheses']
            rows.append(row)
            print(json.dumps({'case': case['identifier'], 'passed': row['passed'],
                              'decision_mode': row['decision_mode']}, ensure_ascii=False), flush=True)
    return {'model': model, 'count': len(rows), 'passed': sum(row['passed'] for row in rows), 'cases': rows,
            'limitations': 'Four public examples, mock platform and simulated memory; no statistical or production generalization claim.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    report = asyncio.run(evaluate())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Model sample results: {report['passed']}/{report['count']}")


if __name__ == '__main__':
    main()
