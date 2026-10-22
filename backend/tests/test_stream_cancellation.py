"""SSE cancellation semantics of the in-process stream generator (P12).

Complements ``tests/test_api_security.py::test_cancelled_stream_task_records_cancelled_and_releases_lock``,
which has to re-arm ``Task.cancel()`` because a single cancellation could be lost.
Every test below cancels **exactly once** and synchronises on product effects
(a task-record write, an event, a finished task) instead of on sleeps.
"""
import asyncio
import contextlib
import json
import sys
import unittest
from unittest import mock

from api import main
from developer.events import publish_existing


async def pump(times: int = 1) -> None:
    """Yield to the event loop `times` times without sleeping on a wall clock."""
    for _ in range(times):
        await asyncio.sleep(0)


async def settle_task(task, timeout: float = 5.0):
    """Await a task's outcome, bounded, without letting its cancellation escape."""
    results = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=timeout)
    return results[0]


class Lock:
    def __init__(self, redis, key):
        self.redis, self.key = redis, key

    async def acquire(self):
        if self.key in self.redis.held:
            return False
        self.redis.held.add(self.key)
        return True

    async def release(self):
        self.redis.held.discard(self.key)


class Redis:
    def __init__(self):
        self.values, self.held, self.sets, self.stream = {}, set(), {}, []

    async def eval(self, *_args):
        return 1

    def lock(self, key, **_kwargs):
        return Lock(self, key)

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def xadd(self, _key, fields, **_kwargs):
        self.stream.append(dict(fields))
        return f'{len(self.stream)}-0'

    def finished_statuses(self, event: str = 'task.finished'):
        return [row['status'] for row in self.stream if row.get('event') == event]


class Assistant:
    """Stands in for DeveloperAssistant: publishes timeline events, then blocks."""

    def __init__(self, *, timeline: int = 1, finishes: bool = False):
        self.redis = Redis()
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.runs = 0
        self.cancelled_runs = 0
        self._timeline = timeline
        self._finishes = finishes

    async def run(self, message, user, conversation):
        self.runs += 1
        publish_existing([{'type': 'plan', 'text': f'计划 {index}'}
                          for index in range(self._timeline)])
        self.entered.set()
        if self._finishes:
            self.resume.set()
        try:
            await self.resume.wait()
        except asyncio.CancelledError:
            self.cancelled_runs += 1
            raise
        result = {'conversation_id': conversation, 'run_id': 'r1', 'answer': message}
        await self.redis.setex('developer:run:r1', 60,
                               json.dumps({'user_id': user, 'response': result}))
        return result


class NextStreamEventTests(unittest.IsolatedAsyncioTestCase):
    """Unit contract of the keep-alive wait that replaced `asyncio.wait_for`."""

    async def test_single_cancel_propagates_when_the_event_is_already_queued(self):
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(('timeline', {'n': 1}))
        get_task = asyncio.ensure_future(queue.get())
        await pump(2)
        self.assertTrue(get_task.done(), 'the queued event should already be dequeued')

        waiter = asyncio.create_task(main._next_stream_event(get_task, timeout=15))
        await pump()
        self.assertFalse(waiter.done(), 'the helper should be parked in its own wait')

        waiter.cancel()  # exactly one cancel, the way Starlette disconnects a client
        outcome = await settle_task(waiter)
        self.assertIsInstance(outcome, asyncio.CancelledError)
        self.assertTrue(waiter.cancelled())
        self.assertEqual(get_task.result(), ('timeline', {'n': 1}),
                         'cancelling the wait must not discard the queued event')

    @unittest.skipIf(sys.version_info >= (3, 11),
                     'Python 3.11 reimplemented wait_for on top of asyncio.timeout, '
                     'which is level-triggered and no longer swallows this cancel')
    async def test_stdlib_wait_for_shape_swallows_the_same_cancel(self):
        # Root-cause control for the pre-P12 form `await asyncio.wait_for(queue.get(), 15)`:
        # when the inner future is already done, 3.10's wait_for returns its result
        # instead of propagating the external cancel, so the stream kept running.
        async def parked(future):
            return await asyncio.wait_for(future, timeout=15)

        loop = asyncio.get_running_loop()
        inner = loop.create_future()
        inner.set_result(('timeline', {'n': 1}))
        task = asyncio.create_task(parked(inner))
        await pump()
        self.assertFalse(task.done())

        task.cancel()
        outcome = await settle_task(task)
        self.assertEqual(outcome, ('timeline', {'n': 1}))
        self.assertFalse(task.cancelled(), 'the cancel was swallowed, not propagated')

    async def test_returns_an_event_published_while_parked(self):
        queue: asyncio.Queue = asyncio.Queue()
        get_task = asyncio.ensure_future(queue.get())
        waiter = asyncio.create_task(main._next_stream_event(get_task, timeout=5))
        await pump()
        self.assertFalse(waiter.done())
        queue.put_nowait(('timeline', {'n': 2}))
        self.assertEqual(await asyncio.wait_for(waiter, timeout=5), ('timeline', {'n': 2}))

    async def test_idle_timeout_leaves_the_getter_armed_and_loses_nothing(self):
        queue: asyncio.Queue = asyncio.Queue()
        get_task = asyncio.ensure_future(queue.get())
        self.assertIsNone(await main._next_stream_event(get_task, timeout=0.02))
        self.assertFalse(get_task.done())
        self.assertFalse(get_task.cancelled())
        queue.put_nowait(('end', {}))
        self.assertEqual(await asyncio.wait_for(main._next_stream_event(get_task, timeout=5),
                                                timeout=5), ('end', {}))

    async def test_cancelling_the_getter_after_an_idle_tick_keeps_the_queue_intact(self):
        queue: asyncio.Queue = asyncio.Queue()
        get_task = asyncio.ensure_future(queue.get())
        self.assertIsNone(await main._next_stream_event(get_task, timeout=0.02))
        queue.put_nowait(('complete', {'answer': 'x'}))
        get_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await get_task
        self.assertEqual(queue.get_nowait(), ('complete', {'answer': 'x'}))


class StreamCancellationTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end behaviour of the SSE generator under a single cancellation."""

    async def assertNoLeakedTasks(self, before):
        leaked = [task for task in asyncio.all_tasks() - before if not task.done()]
        if leaked:
            await asyncio.wait_for(asyncio.gather(*leaked, return_exceptions=True), timeout=5)
        still = [task for task in leaked if not task.done()]
        self.assertEqual(still, [], f'stream left {len(still)} task(s) running')

    async def open_stream(self, assistant, conversation):
        stream = await main.developer_chat_stream(
            main.DeveloperChatRequest(message='hello', conversation_id=conversation), 'alice', None)
        iterator = stream.body_iterator
        started = await anext(iterator)
        self.assertIn('event: started', started)
        return iterator, json.loads(started.split('data: ', 1)[1])['task_id']

    def watch_cancelled_record(self, task_id):
        """Spy on the task-record write and return (patcher, event)."""
        event = asyncio.Event()
        original = main._write_task_record

        async def spy(redis, id_, user_id, status, run_id=None, workspace=None, trace_id=None):
            record = await original(redis, id_, user_id, status, run_id,
                                    workspace=workspace, trace_id=trace_id)
            if id_ == task_id and status == 'cancelled':
                event.set()
            return record

        return mock.patch.object(main, '_write_task_record', spy), event

    async def test_single_cancel_ends_the_stream_and_releases_the_session_lock(self):
        assistant = Assistant()
        before = set(asyncio.all_tasks())
        with mock.patch.object(main, '_developer', assistant):
            iterator, task_id = await self.open_stream(assistant, 'c-p12-cancel')
            patcher, cancelled_write = self.watch_cancelled_record(task_id)

            async def consume():
                async for _ in iterator:
                    pass

            with patcher:
                consumer = asyncio.create_task(consume())
                await asyncio.wait_for(assistant.entered.wait(), timeout=5)
                consumer.cancel()  # once; the pre-P12 wait_for could swallow exactly this
                await asyncio.wait_for(cancelled_write.wait(), timeout=5)
                outcome = await settle_task(consumer)
            self.assertIsInstance(outcome, asyncio.CancelledError)
            self.assertTrue(consumer.cancelled())

        record = json.loads(assistant.redis.values[f'developer:task:{task_id}'])
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(assistant.redis.finished_statuses(), ['cancelled'])
        self.assertFalse(assistant.redis.held, 'the session lock must be released')
        self.assertEqual(assistant.runs, 1)
        self.assertEqual(assistant.cancelled_runs, 1, 'the run must be cancelled, not finished')
        await self.assertNoLeakedTasks(before)

    async def test_cancel_is_propagated_even_when_events_are_queued(self):
        # The swallow window in the old code needed an event already dequeued by the
        # pending `queue.get()`; publish several so the generator is mid-drain.
        assistant = Assistant(timeline=4)
        before = set(asyncio.all_tasks())
        with mock.patch.object(main, '_developer', assistant):
            iterator, task_id = await self.open_stream(assistant, 'c-p12-busy')
            patcher, cancelled_write = self.watch_cancelled_record(task_id)

            async def consume():
                async for _ in iterator:
                    pass

            with patcher:
                consumer = asyncio.create_task(consume())
                await asyncio.wait_for(assistant.entered.wait(), timeout=5)
                consumer.cancel()
                await asyncio.wait_for(cancelled_write.wait(), timeout=5)
                await settle_task(consumer)

        self.assertEqual(json.loads(assistant.redis.values[f'developer:task:{task_id}'])['status'],
                         'cancelled')
        self.assertEqual(assistant.redis.finished_statuses(), ['cancelled'])
        self.assertFalse(assistant.redis.held)
        await self.assertNoLeakedTasks(before)

    async def test_repeated_single_cancels_all_end_the_stream(self):
        for trial in range(6):
            with self.subTest(trial=trial):
                assistant = Assistant(timeline=2)
                with mock.patch.object(main, '_developer', assistant):
                    iterator, task_id = await self.open_stream(assistant, f'c-p12-stress-{trial}')
                    patcher, cancelled_write = self.watch_cancelled_record(task_id)

                    async def consume():
                        async for _ in iterator:
                            pass

                    with patcher:
                        consumer = asyncio.create_task(consume())
                        await asyncio.wait_for(assistant.entered.wait(), timeout=5)
                        consumer.cancel()
                        await asyncio.wait_for(cancelled_write.wait(), timeout=5)
                        await settle_task(consumer)
                self.assertEqual(
                    json.loads(assistant.redis.values[f'developer:task:{task_id}'])['status'],
                    'cancelled')
                self.assertFalse(assistant.redis.held)

    async def test_idle_heartbeat_still_fires_and_refreshes_the_task(self):
        assistant = Assistant()
        touched = asyncio.Event()
        original_touch = main._touch_task

        async def spy_touch(redis, task_id):
            await original_touch(redis, task_id)
            touched.set()

        with mock.patch.object(main, '_developer', assistant), \
                mock.patch.object(main, 'SSE_HEARTBEAT_SECONDS', 0.05), \
                mock.patch.object(main, '_touch_task', spy_touch):
            iterator, task_id = await self.open_stream(assistant, 'c-p12-heartbeat')
            chunk = await asyncio.wait_for(anext(iterator), timeout=5)
            self.assertIn('event: timeline', chunk)
            beat = await asyncio.wait_for(anext(iterator), timeout=5)
            self.assertEqual(beat, ': heartbeat\n\n')
            second_beat = await asyncio.wait_for(anext(iterator), timeout=5)
            self.assertEqual(second_beat, ': heartbeat\n\n')
            await asyncio.wait_for(touched.wait(), timeout=5)
            record = json.loads(assistant.redis.values[f'developer:task:{task_id}'])
            self.assertEqual(record['status'], 'running')
            self.assertFalse(assistant.redis.finished_statuses())
            await asyncio.wait_for(iterator.aclose(), timeout=10)
        self.assertFalse(assistant.redis.held)
        self.assertEqual(json.loads(assistant.redis.values[f'developer:task:{task_id}'])['status'],
                         'cancelled')

    async def test_complete_path_keeps_its_event_order_and_leaves_no_task_behind(self):
        assistant = Assistant(timeline=2, finishes=True)
        before = set(asyncio.all_tasks())
        with mock.patch.object(main, '_developer', assistant):
            iterator, task_id = await self.open_stream(assistant, 'c-p12-complete')

            async def collect():
                return [chunk async for chunk in iterator]

            # Bounded so a generator that never ends fails instead of hanging the suite.
            chunks = await asyncio.wait_for(collect(), timeout=10)

        self.assertEqual([chunk.split('\n', 1)[0] for chunk in chunks],
                         ['event: timeline', 'event: timeline', 'event: complete'])
        self.assertNotIn(': heartbeat\n\n', chunks)
        payload = json.loads(chunks[-1].split('data: ', 1)[1])
        self.assertEqual(payload['run_id'], 'r1')
        record = json.loads(assistant.redis.values[f'developer:task:{task_id}'])
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['run_id'], 'r1')
        self.assertEqual(assistant.redis.finished_statuses(), ['completed'])
        self.assertFalse(assistant.redis.held)
        await self.assertNoLeakedTasks(before)

    async def test_each_wait_returns_the_next_event_not_a_repeat(self):
        # Guards the re-arm inside the generator loop: a getter that is reused after
        # its event was taken would replay the same event forever.
        assistant = Assistant(timeline=3, finishes=True)
        with mock.patch.object(main, '_developer', assistant):
            iterator, _task_id = await self.open_stream(assistant, 'c-p12-rearm')

            async def collect():
                return [chunk async for chunk in iterator]

            chunks = await asyncio.wait_for(collect(), timeout=10)

        self.assertEqual([chunk.split('\n', 1)[0] for chunk in chunks],
                         ['event: timeline'] * 3 + ['event: complete'])
        plans = [json.loads(chunk.split('data: ', 1)[1])['text']
                 for chunk in chunks if chunk.startswith('event: timeline')]
        self.assertEqual(plans, ['计划 0', '计划 1', '计划 2'])

    async def test_client_disconnect_via_aclose_still_records_cancelled(self):
        assistant = Assistant()
        with mock.patch.object(main, '_developer', assistant):
            iterator, task_id = await self.open_stream(assistant, 'c-p12-aclose')
            await asyncio.wait_for(anext(iterator), timeout=5)
            await asyncio.wait_for(iterator.aclose(), timeout=10)
        self.assertEqual(json.loads(assistant.redis.values[f'developer:task:{task_id}'])['status'],
                         'cancelled')
        self.assertEqual(assistant.redis.finished_statuses(), ['cancelled'])
        self.assertFalse(assistant.redis.held)


if __name__ == '__main__':
    unittest.main()
