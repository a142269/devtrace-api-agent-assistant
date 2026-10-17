"""Compare fixed and conditional scope inspection on the public simulated baseline."""
import argparse
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
from developer import sandbox
from developer.assistant import DeveloperAssistant, RuleDecisionModel
from developer.data import DOCS
from developer.evaluate import EVAL_CASES, assess
from mcp.tool_manager import MCPToolManager


class Redis:
    def __init__(self):
        self.values = {}
    async def get(self, key):
        return self.values.get(key)
    async def setex(self, key, ttl, value):
        self.values[key] = value
    async def zadd(self, key, mapping):
        pass
    async def zremrangebyrank(self, key, start, end):
        pass
    async def expire(self, key, ttl):
        pass


class Memory:
    recent_messages = []
    relevant_history = []
    summary = ''
    user_profile = {}
    async def get_context(self, user_id, conversation_id, query=''):
        return self
    def to_prompt_text(self):
        return ''
    async def add_message(self, user_id, conversation_id, role, content):
        pass


class Knowledge:
    async def search_async(self, query, top_k=4):
        return [{'title': doc['title'], 'content': doc['content']} for doc in DOCS[:top_k]]


class SandboxClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs.pop('timeout', None)
        super().__init__(transport=httpx.ASGITransport(app=sandbox.app), base_url='http://sandbox', **kwargs)


async def run_suite(dynamic_scope):
    assistant = DeveloperAssistant(memory=Memory(), knowledge=Knowledge(), redis_client=Redis(),
        model=RuleDecisionModel(), tool_manager=MCPToolManager(api_key='evaluation-only'),
        sandbox_url='http://sandbox', dynamic_scope=dynamic_scope)
    rows = []
    with patch('developer.assistant.httpx.AsyncClient', SandboxClient):
        for case in EVAL_CASES:
            response = await assistant.run(case['message'], 'eval')
            rows.append(assess(case, response, 'rules'))
    count = len(rows)
    return {'count': count, 'passed': sum(row['passed'] for row in rows),
            'diagnosis_correct': sum(row['diagnosis_correct'] is True for row in rows),
            'diagnosis_applicable': sum(row['diagnosis_correct'] is not None for row in rows),
            'evidence_supported': sum(row['evidence_supported'] for row in rows),
            'mean_tool_calls': sum(row['tool_actions'] for row in rows) / count if count else None,
            'cases': rows}


async def evaluate():
    return {'fixed': await run_suite(False), 'dynamic': await run_suite(True),
            'limitations': 'Both approaches use rules and the same public generated platform cases; no model-generalization claim.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: {k: v for k, v in value.items() if k != 'cases'}
                      for key, value in report.items() if isinstance(value, dict)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
