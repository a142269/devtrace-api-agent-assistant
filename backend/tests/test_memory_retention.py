"""P10: Chroma episodic retention CLI.

The CLI is the only path that removes cross-session history, so the tests pin the
boundaries: dry-run by default, --apply deletes only rows older than the cutoff, rows
without a usable ts are skipped rather than guessed at, --user only narrows the scope,
profile collections are refused and never read, and any Chroma fault exits non-zero.
"""
import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta
from developer import memory_retention as retention

NOW = datetime(2026, 10, 22, 12, 0, 0)


def row(doc_id, user, conv, ts):
    return {'id': doc_id, 'metadata': {'user_id': user, 'conv_id': conv, 'ts': ts}}


class FakeCollection:
    def __init__(self, rows, *, name='developer_episodic', fail_get=False, fail_delete=False):
        self.rows = rows
        self.name = name
        self.fail_get = fail_get
        self.fail_delete = fail_delete
        self.get_calls = 0
        self.delete_calls = []

    def get(self, include=None):
        self.get_calls += 1
        if self.fail_get:
            raise ConnectionError('Chroma server unreachable')
        return {'ids': [item['id'] for item in self.rows],
                'metadatas': [item['metadata'] for item in self.rows]}

    def delete(self, ids=None):
        if self.fail_delete:
            raise RuntimeError('Chroma rejected the delete batch')
        self.delete_calls.append(list(ids))
        keep = set(ids)
        self.rows = [item for item in self.rows if item['id'] not in keep]

    def deleted_ids(self):
        return [doc_id for batch in self.delete_calls for doc_id in batch]


class FakeClient:
    def __init__(self, **collections):
        self.collections = collections
        self.requested = []

    def get_or_create_collection(self, name, **_kwargs):
        self.requested.append(name)
        if name not in self.collections:
            raise KeyError(f'no such collection in test: {name}')
        return self.collections[name]


class StorageErrorClient(FakeClient):
    def get_or_create_collection(self, name, **_kwargs):
        raise ConnectionError('Chroma is down')


def iso(moment):
    return moment.isoformat()


class RetentionPlanTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = NOW - timedelta(days=30)

    def test_old_rows_become_candidates_and_new_rows_stay(self):
        plan = retention.classify([
            row('old-1', 'alice', 'c1', iso(self.cutoff - timedelta(seconds=1))),
            row('edge', 'alice', 'c1', iso(self.cutoff)),
            row('new', 'bob', 'c2', iso(NOW - timedelta(days=1))),
        ], cutoff=self.cutoff)
        self.assertEqual(plan['candidates'], ['old-1'])
        self.assertEqual(plan['kept_fresh'], 2)
        self.assertEqual(plan['by_user'], {'alice': 1})
        self.assertEqual(plan['conversations'], ['c1'])

    def test_rows_without_usable_ts_are_skipped_not_guessed(self):
        plan = retention.classify([
            row('no-ts', 'alice', 'c1', None),
            row('blank', 'alice', 'c1', '   '),
            row('junk', 'alice', 'c1', 'not-a-date'),
            row('epoch', 'alice', 'c1', 1_600_000_000),
        ], cutoff=self.cutoff)
        self.assertEqual(sorted(plan['skipped_no_ts']), ['blank', 'junk', 'no-ts'])
        self.assertEqual(plan['candidates'], ['epoch'])

    def test_user_filter_only_narrows(self):
        plan = retention.classify([
            row('old-a', 'alice', 'c1', iso(self.cutoff - timedelta(days=1))),
            row('old-b', 'bob', 'c2', iso(self.cutoff - timedelta(days=1))),
        ], cutoff=self.cutoff, user='alice')
        self.assertEqual(plan['candidates'], ['old-a'])
        self.assertEqual(plan['out_of_scope'], 1)


class RunRetentionTests(unittest.TestCase):
    def test_dry_run_reports_but_deletes_nothing(self):
        collection = FakeCollection([
            row('old', 'alice', 'c1', iso(NOW - timedelta(days=400))),
            row('new', 'alice', 'c2', iso(NOW - timedelta(days=2))),
            row('undated', 'bob', 'c3', None),
        ])
        report = retention.run_retention(collection, days=30, now=NOW)
        self.assertEqual(report['mode'], 'dry-run')
        self.assertEqual(report['candidates'], 1)
        self.assertEqual(report['deleted'], 0)
        self.assertEqual(report['skipped_no_ts'], 1)
        self.assertEqual(collection.delete_calls, [])
        self.assertEqual(len(collection.rows), 3)

    def test_apply_deletes_only_expired_and_keeps_everything_else(self):
        collection = FakeCollection([
            row('old-1', 'alice', 'c1', iso(NOW - timedelta(days=400))),
            row('old-2', 'bob', 'c2', iso(NOW - timedelta(days=31, seconds=1))),
            row('edge', 'alice', 'c1', iso(NOW - timedelta(days=30))),
            row('new', 'alice', 'c2', iso(NOW - timedelta(hours=1))),
            row('undated', 'carol', 'c3', 'oops'),
        ])
        report = retention.run_retention(collection, days=30, apply=True, now=NOW)
        self.assertEqual(report['mode'], 'apply')
        self.assertEqual(report['deleted'], 2)
        self.assertEqual(sorted(collection.deleted_ids()), ['old-1', 'old-2'])
        self.assertEqual(sorted(item['id'] for item in collection.rows),
                         ['edge', 'new', 'undated'])
        self.assertFalse(report['profile_collection_touched'])

    def test_apply_with_user_filter_spares_other_users(self):
        collection = FakeCollection([
            row('old-alice', 'alice', 'c1', iso(NOW - timedelta(days=90))),
            row('old-bob', 'bob', 'c2', iso(NOW - timedelta(days=90))),
        ])
        report = retention.run_retention(collection, days=30, apply=True, user='alice', now=NOW)
        self.assertEqual(report['deleted'], 1)
        self.assertEqual(collection.deleted_ids(), ['old-alice'])
        self.assertEqual([item['id'] for item in collection.rows], ['old-bob'])

    def test_zero_days_is_rejected_before_any_read(self):
        collection = FakeCollection([])
        with self.assertRaises(retention.RetentionError):
            retention.run_retention(collection, days=0, now=NOW)
        self.assertEqual(collection.get_calls, 0)


class CliTests(unittest.TestCase):
    def build(self, *rows, **kwargs):
        episodic = FakeCollection(list(rows), **kwargs)
        profile = FakeCollection([row('profile-1', 'alice', 'c1', iso(NOW))],
                                 name='developer_profile')
        client = FakeClient(developer_episodic=episodic, developer_profile=profile)
        return client, episodic, profile

    def run_cli(self, argv, client):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = retention.main(argv, client_factory=lambda: client)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_is_the_default_and_prints_json_report(self):
        client, episodic, _profile = self.build(
            row('old', 'alice', 'c1', iso(NOW - timedelta(days=100))))
        code, out, err = self.run_cli(['--days', '30', '--json'], client)
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report['mode'], 'dry-run')
        self.assertEqual(report['candidates'], 1)
        self.assertEqual(report['deleted'], 0)
        self.assertEqual(episodic.delete_calls, [])
        self.assertEqual(err, '')

    def test_text_output_mentions_the_boundary(self):
        client, _episodic, _profile = self.build(
            row('old', 'alice', 'c1', iso(NOW - timedelta(days=100))))
        code, out, _err = self.run_cli(['--days', '30'], client)
        self.assertEqual(code, 0)
        self.assertIn('dry-run', out)
        self.assertIn('未删除任何记录', out)
        self.assertIn('画像', out)

    def test_invalid_days_exits_with_usage_error(self):
        client, _episodic, _profile = self.build()
        for bad in ('0', '-1', 'abc', '3.5'):
            with self.assertRaises(SystemExit) as caught:
                retention.main(['--days', bad], client_factory=lambda: client)
            self.assertEqual(caught.exception.code, 2)

    def test_dry_run_and_apply_are_mutually_exclusive(self):
        client, _episodic, _profile = self.build()
        with self.assertRaises(SystemExit) as caught:
            retention.main(['--days', '30', '--dry-run', '--apply'],
                           client_factory=lambda: client)
        self.assertEqual(caught.exception.code, 2)

    def test_chroma_outage_fails_readably_and_non_zero(self):
        client = StorageErrorClient(developer_episodic=FakeCollection([]))
        code, _out, err = self.run_cli(['--days', '30'], client)
        self.assertEqual(code, 1)
        self.assertIn('Chroma', err)

    def test_read_failure_is_not_reported_as_success(self):
        client, episodic, _profile = self.build(fail_get=True)
        code, _out, err = self.run_cli(['--days', '30'], client)
        self.assertEqual(code, 1)
        self.assertIn('读取情景记忆失败', err)
        self.assertEqual(episodic.delete_calls, [])

    def test_delete_failure_reports_partial_progress(self):
        client, episodic, _profile = self.build(
            row('old', 'alice', 'c1', iso(NOW - timedelta(days=100))), fail_delete=True)
        code, _out, err = self.run_cli(['--days', '30', '--apply'], client)
        self.assertEqual(code, 1)
        self.assertIn('已删除 0/1', err)

    def test_profile_collection_is_refused_and_never_read(self):
        client, episodic, profile = self.build(
            row('old', 'alice', 'c1', iso(NOW - timedelta(days=100))))
        code, _out, err = self.run_cli(['--days', '30', '--apply', '--collection',
                                       'developer_profile'], client)
        self.assertEqual(code, 1)
        self.assertIn('拒绝', err)
        self.assertEqual(profile.delete_calls, [])
        self.assertEqual(profile.get_calls, 0)
        self.assertEqual(episodic.delete_calls, [])
        self.assertEqual(client.requested, [])

    def test_guard_rejects_any_profile_name(self):
        for name in ('user_profile', 'Developer_Profile', 'developer_episodic'):
            if 'profile' in name.lower():
                with self.assertRaises(retention.RetentionError):
                    retention.guard_collection(name)
            else:
                self.assertEqual(retention.guard_collection(name), name)


if __name__ == '__main__':
    unittest.main()
