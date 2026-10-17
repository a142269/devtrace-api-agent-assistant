"""Inject one transient failure per simulated case and measure manual plan recovery."""
import argparse
import asyncio
import json
import math
import time
from pathlib import Path
from unittest.mock import patch

from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.evaluate_planning import Knowledge, Memory, Redis, SandboxClient
from mcp.tool_manager import MCPToolManager


async def evaluate():
    rows = []
    with patch('developer.assistant.httpx.AsyncClient', SandboxClient):
        for fault in ('exception', 'timeout'):
            for index in range(1, 11):
                assistant = DeveloperAssistant(memory=Memory(), knowledge=Knowledge(), redis_client=Redis(),
                    model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key='evaluation-only'),
                    sandbox_url='http://sandbox')
                tool = assistant.tools._tools['get_request_trace']
                original = tool.handler
                calls = 0
                async def fail_once(params, context):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        if fault == 'timeout':
                            raise asyncio.TimeoutError()
                        raise RuntimeError('injected service failure')
                    return await original(params, context)
                tool.handler = fail_once
                started = time.perf_counter()
                first = await assistant.run(f'请求 req_quota_{index:02d} 返回 429', 'eval')
                second = await assistant.run('继续', 'eval', first['conversation_id'])
                hypotheses = second['diagnosis']['hypotheses']
                rows.append({'case': f'{fault}-{index:02d}', 'fault': fault,
                             'reported_failure': first['plan']['status'] == 'waiting' and any(not a['success'] for a in first['actions']),
                             'recovered': second['plan']['status'] == 'complete' and any(h['code'] == 'minute_quota_exceeded' for h in hypotheses),
                             'elapsed_ms': round((time.perf_counter() - started) * 1000, 1)})
    ordered = sorted(row['elapsed_ms'] for row in rows)
    return {'count': len(rows), 'failure_reported': sum(row['reported_failure'] for row in rows),
            'recovered': sum(row['recovered'] for row in rows),
            'p95_two_turn_ms': ordered[math.ceil(len(ordered) * .95) - 1] if ordered else None,
            'cases': rows,
            'limitations': 'In-process mock service and fake Redis; injected failures recover after one user retry. Latency is not production performance.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'cases'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
