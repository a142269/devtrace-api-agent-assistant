"""Tests for the Redis-backed task queue and worker retry semantics."""
import asyncio
import json
import unittest

from api import worker
from developer import task_queue
from developer.events import publish_existing


class QueueRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}
        self.sets = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)
        self.lists.pop(key, None)

    async def expire(self, _key, _ttl):
        return True

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def lrange(self, key, start, end):
        rows = self.lists.get(key, [])
        if end == -1:
            return rows[start:]
        return rows[start:end + 1]

    async def brpop(self, key, timeout=0):
        rows = self.lists.get(key, [])
        if not rows:
            return None
        return key, rows.pop()


class FakeAssistant:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0

    async def run(self, message, user_id, conversation_id):
        self.calls += 1
        timeline = publish_existing([{'type': 'plan', 'text': '测试计划'}])
        timeline.append({'type': 'action', 'text': '测试行动'})
        if self.calls <= self.failures:
            raise RuntimeError('transient failure')
        await asyncio.sleep(0)
        result = {'conversation_id': conversation_id, 'run_id': f'run-{self.calls}',
                  'answer': 'ok', 'plan': {'status': 'complete'}}
        return result


class TaskQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = QueueRedis()

    async def enqueue(self, task_id='t1', user='alice', conversation='c1'):
        claimed = await task_queue.try_claim_session(self.redis, user, conversation, task_id)
        self.assertTrue(claimed)
        await task_queue.enqueue(self.redis, task_id, user, '你好', conversation)

    async def test_worker_completes_task_and_publishes_events(self):
        await self.enqueue()
        assistant = FakeAssistant()
        await worker.process_one(assistant, self.redis, 't1')
        await asyncio.sleep(0.05)
        record = await task_queue.get_task(self.redis, 't1')
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['run_id'], 'run-1')
        self.assertEqual(assistant.calls, 1)
        events = await task_queue.read_events(self.redis, 't1')
        self.assertTrue(events)
        self.assertTrue(all(json.loads(row)['type'] in {'plan', 'action'} for row in events))
        self.assertFalse(await self.redis.get(task_queue.session_key('alice', 'c1')))

    async def test_transient_failure_retries_then_dead_letters(self):
        await self.enqueue('t2', 'alice', 'c2')
        failing = FakeAssistant(failures=2)
        await worker.process_one(failing, self.redis, 't2')
        record = await task_queue.get_task(self.redis, 't2')
        self.assertEqual(record['status'], 'queued')
        self.assertEqual(record['attempts'], 1)
        # Retry runs again and exhausts the attempt budget -> dead letter.
        await worker.process_one(failing, self.redis, 't2')
        record = await task_queue.get_task(self.redis, 't2')
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['attempts'], 2)
        self.assertIn('t2', self.redis.lists[task_queue.DEAD_KEY])
        self.assertFalse(await self.redis.get(task_queue.session_key('alice', 'c2')))

    async def test_success_on_retry_clears_error_and_releases_session(self):
        await self.enqueue('t3', 'alice', 'c3')
        flaky = FakeAssistant(failures=1)
        await worker.process_one(flaky, self.redis, 't3')
        record = await task_queue.get_task(self.redis, 't3')
        self.assertEqual(record['status'], 'queued')
        await worker.process_one(flaky, self.redis, 't3')
        record = await task_queue.get_task(self.redis, 't3')
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['attempts'], 1)
        self.assertIsNone(record.get('error'))

    async def test_cancel_requested_before_execution(self):
        await self.enqueue('t4', 'alice', 'c4')
        await task_queue.request_cancel(self.redis, 't4')
        assistant = FakeAssistant()
        await worker.process_one(assistant, self.redis, 't4')
        record = await task_queue.get_task(self.redis, 't4')
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(assistant.calls, 0)
        self.assertFalse(await self.redis.get(task_queue.session_key('alice', 'c4')))

    async def test_same_session_cannot_enqueue_twice(self):
        await self.enqueue('t5', 'alice', 'c5')
        claimed = await task_queue.try_claim_session(self.redis, 'alice', 'c5', 't6')
        self.assertFalse(claimed)
        await task_queue.release_session(self.redis, 'alice', 'c5')
        claimed = await task_queue.try_claim_session(self.redis, 'alice', 'c5', 't6')
        self.assertTrue(claimed)

    async def test_recover_running_task_from_dead_worker(self):
        await self.enqueue('t7', 'alice', 'c7')
        self.redis.lists.setdefault(task_queue.QUEUE_KEY, []).clear()
        await task_queue.update_task(self.redis, 't7', status='running', worker_id='dead-worker')

        async def dead(_redis, _worker):
            return False

        recovered = await task_queue.recover_running_tasks(self.redis, dead)
        self.assertEqual(recovered, 1)
        record = await task_queue.get_task(self.redis, 't7')
        self.assertEqual(record['status'], 'queued')
        self.assertEqual(record['attempts'], 1)
        self.assertIn('t7', self.redis.lists[task_queue.QUEUE_KEY])

    async def test_recover_does_not_touch_a_live_worker_task(self):
        await self.enqueue('t8', 'alice', 'c8')
        await task_queue.update_task(self.redis, 't8', status='running', worker_id='live-worker')

        async def alive(_redis, _worker):
            return True

        recovered = await task_queue.recover_running_tasks(self.redis, alive)
        self.assertEqual(recovered, 0)
        record = await task_queue.get_task(self.redis, 't8')
        self.assertEqual(record['status'], 'running')


if __name__ == '__main__':
    unittest.main()
