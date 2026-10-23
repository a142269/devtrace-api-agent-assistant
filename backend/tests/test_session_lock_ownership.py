"""P13 session-lock ownership: a task may only ever release the lock it owns.

``try_claim_session`` stores the task id as the lock value, but until P13 every
terminal cleanup deleted the key unconditionally, so a delayed release (a slow
worker finishing after the conversation moved on) dropped a live task's lock and
let a third task run the same conversation concurrently. These tests pin the
compare-and-delete contract at every release point.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api import main
from api import worker
from developer import task_queue

from test_queue_production import ProdRedis


class TakeoverAssistant:
    """Hands the conversation to another task while this one is still running."""

    def __init__(self, redis, new_owner='t-B', fail=False):
        self.redis = redis
        self.new_owner = new_owner
        self.fail = fail

    async def run(self, message, user_id, conversation_id):
        await task_queue.release_session(self.redis, user_id, conversation_id)
        await task_queue.try_claim_session(self.redis, user_id, conversation_id, self.new_owner)
        if self.fail:
            raise RuntimeError('boom')
        return {'conversation_id': conversation_id, 'run_id': 'run-B',
                'answer': 'ok', 'plan': {'status': 'complete'}}


class PlainAssistant:
    def __init__(self, fail=False):
        self.fail = fail

    async def run(self, message, user_id, conversation_id):
        if self.fail:
            raise RuntimeError('boom')
        return {'conversation_id': conversation_id, 'run_id': 'run-A',
                'answer': 'ok', 'plan': {'status': 'complete'}}


class SessionLockOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = ProdRedis()
        self.key = task_queue.session_key('alice', 'c1')

    async def claim(self, task_id, user='alice', conversation='c1'):
        self.assertTrue(await task_queue.try_claim_session(self.redis, user, conversation, task_id))

    async def owner(self):
        value = await self.redis.get(self.key)
        return value

    async def test_owner_release_deletes_the_lock_and_returns_one(self):
        await self.claim('t-A')
        self.assertEqual(await task_queue.release_session_if_owner(
            self.redis, 'alice', 'c1', 't-A'), 1)
        self.assertIsNone(await self.owner())

    async def test_non_owner_release_returns_zero_and_never_touches_the_lock(self):
        await self.claim('t-A')
        self.assertEqual(await task_queue.release_session_if_owner(
            self.redis, 'alice', 'c1', 't-B'), 0)
        self.assertEqual(await self.owner(), 't-A')

    async def test_releasing_an_already_gone_lock_returns_zero(self):
        self.assertEqual(await task_queue.release_session_if_owner(
            self.redis, 'alice', 'c1', 't-A'), 0)

    async def test_release_sends_the_documented_script_key_and_argv(self):
        """The fake emulates compare-and-delete; this pins what Redis actually runs."""
        sent = []

        async def spy(script, numkeys, key, *argv):
            sent.append((script, numkeys, key, argv))
            return 0

        self.redis.eval = spy
        await task_queue.release_session_if_owner(self.redis, 'alice', 'c1', 't-A')
        script, numkeys, key, argv = sent[0]
        self.assertEqual(numkeys, 1)
        self.assertEqual(key, task_queue.session_key('alice', 'c1'))
        self.assertEqual(argv, ('t-A',))
        self.assertIn("redis.call('GET',KEYS[1]) == ARGV[1]", script)
        self.assertIn("redis.call('DEL',KEYS[1])", script)
        self.assertIn('else return 0 end', script)

    async def test_retained_release_primitive_is_still_unconditional(self):
        await self.claim('t-A')
        await task_queue.release_session(self.redis, 'alice', 'c1')
        self.assertIsNone(await self.owner())

    async def test_blind_primitive_clobbers_the_newer_lock_the_owner_aware_one_does_not(self):
        """Negative control: this is the P12 gap, reproduced on the same fixture."""
        await self.claim('t-A')
        await self.redis.delete(self.key)
        await task_queue.try_claim_session(self.redis, 'alice', 'c1', 't-B')
        # A's late cleanup with the old unconditional primitive drops B's lock.
        await task_queue.release_session(self.redis, 'alice', 'c1')
        self.assertIsNone(await self.owner())
        # The owner-aware call for the same late task leaves it in place instead.
        await self.redis.delete(self.key)
        await task_queue.try_claim_session(self.redis, 'alice', 'c1', 't-B')
        self.assertEqual(await task_queue.release_session_if_owner(
            self.redis, 'alice', 'c1', 't-A'), 0)
        self.assertEqual(await self.owner(), 't-B')

    async def test_late_completed_release_does_not_clobber_a_newer_task(self):
        await self.claim('t-A')
        await task_queue.enqueue(self.redis, 't-A', 'alice', '你好', 'c1')
        await worker.process_one(TakeoverAssistant(self.redis), self.redis, 't-A')
        # B owns the conversation, so A's terminal cleanup must leave it alone.
        self.assertEqual(await self.owner(), 't-B')
        # ...which keeps the isolation: C cannot claim while B runs.
        self.assertFalse(await task_queue.try_claim_session(self.redis, 'alice', 'c1', 't-C'))
        self.assertEqual(await task_queue.release_session_if_owner(
            self.redis, 'alice', 'c1', 't-B'), 1)
        self.assertTrue(await task_queue.try_claim_session(self.redis, 'alice', 'c1', 't-C'))

    async def test_every_terminal_path_respects_another_tasks_lock(self):
        for case, assistant_kwargs in (('completed', {}), ('failed', {'fail': True})):
            with self.subTest(case=case):
                redis = ProdRedis()
                task_id = f'late-{case}'
                await task_queue.try_claim_session(redis, 'alice', 'c2', task_id)
                await task_queue.enqueue(redis, task_id, 'alice', '你好', 'c2')
                if case == 'failed':
                    # One attempt already spent, so this failure is terminal.
                    await task_queue.update_task(redis, task_id, attempts=1)
                await worker.process_one(TakeoverAssistant(redis, new_owner='t-live',
                                                            **assistant_kwargs), redis, task_id)
                self.assertEqual(await redis.get(task_queue.session_key('alice', 'c2')), 't-live')
                finished = redis.events('task.finished')
                self.assertEqual([row['status'] for row in finished], [
                    'failed' if case == 'failed' else 'completed'])

    async def test_cancelled_before_execution_leaves_a_newer_lock_alone(self):
        redis = ProdRedis()
        await task_queue.try_claim_session(redis, 'alice', 'c3', 't-A')
        await task_queue.enqueue(redis, 't-A', 'alice', '你好', 'c3')
        await task_queue.request_cancel(redis, 't-A')
        # Another task owns the conversation by the time the cancel cleanup runs.
        await redis.set(task_queue.session_key('alice', 'c3'), 't-B')
        await worker.process_one(PlainAssistant(), redis, 't-A')
        self.assertEqual(await redis.get(task_queue.session_key('alice', 'c3')), 't-B')
        self.assertEqual((await task_queue.get_task(redis, 't-A'))['status'], 'cancelled')

    async def test_owner_terminal_release_still_clears_the_lock(self):
        """The stricter rule must not leave the owner's own lock behind."""
        await self.claim('t-A')
        await task_queue.enqueue(self.redis, 't-A', 'alice', '你好', 'c1')
        await worker.process_one(PlainAssistant(), self.redis, 't-A')
        self.assertIsNone(await self.owner())
        self.assertTrue(await task_queue.try_claim_session(self.redis, 'alice', 'c1', 't-B'))

    async def _queued_stream_with(self, enqueue_boom):
        redis = ProdRedis()
        old = main._developer
        main._developer = SimpleNamespace(redis=redis)
        req = main.DeveloperChatRequest(message='req_auth_01 为什么 401', conversation_id='c9')
        try:
            with patch.object(task_queue, 'enqueue', enqueue_boom):
                with self.assertRaises(RuntimeError):
                    await main._queued_stream(req, 'alice', None)
        finally:
            main._developer = old
        return redis

    async def test_enqueue_rollback_releases_only_its_own_lock(self):
        async def boom(_redis, _task_id, *_args, **_kwargs):
            raise RuntimeError('redis down')

        redis = await self._queued_stream_with(boom)
        # Nothing holds the conversation any more: the rolled-back claim is gone.
        self.assertIsNone(await redis.get(task_queue.session_key('alice', 'c9')))

    async def test_enqueue_rollback_leaves_a_newer_lock_alone(self):
        async def boom_takeover(redis, task_id, *_args, **_kwargs):
            # The conversation moved on while this request was enqueueing.
            await task_queue.release_session(redis, 'alice', 'c9')
            await task_queue.try_claim_session(redis, 'alice', 'c9', 't-newer')
            raise RuntimeError('redis down')

        redis = await self._queued_stream_with(boom_takeover)
        self.assertEqual(await redis.get(task_queue.session_key('alice', 'c9')), 't-newer')


if __name__ == '__main__':
    unittest.main()
