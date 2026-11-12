"""Prune preserves deletion fences and yields between bounded transcript deletions."""
from contextlib import closing

import json

import pytest

from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX
from hermes_state_mutation_retirement import RETIRED_PREFIX, prune_retired_tombstones


def ended(db, sid, messages=1, **fields):
    db.create_session(sid, source=fields.pop('source', 'cron'), **fields)
    for n in range(messages):
        db.append_message(sid, 'tool', f'message {n}', timestamp=1)
    db._write_sql('UPDATE sessions SET started_at=1,last_activity_at=1,ended_at=2 WHERE id=?', (sid,))


def local_group(db, ids, request='ordinary'):
    for sid in ids:
        ended(db, sid, chat_id=ids[0])
    db.set_meta(POLICY_PREFIX + ids[0], json.dumps({
        'session_id': ids[0], 'request_id': request, 'lineage': ids,
        'entry': {'session_id': ids[-1]}, 'policy': {'request_json': 'private'}}))


def test_sweep_characterization_local_group_is_whole_and_derived_fence_is_permanent(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        local_group(db, ['owner', 'child'], 'bot-chat:owned')
        db._write_sql('UPDATE sessions SET pinned=1 WHERE id=?', ('child',))
        assert db.prune_sessions(older_than_days=None) == 0
        assert db.get_meta(POLICY_PREFIX + 'owner') is not None
        db._write_sql('UPDATE sessions SET pinned=0 WHERE id=?', ('child',))
        assert db.prune_sessions(older_than_days=None) == 2
        assert db.get_meta(POLICY_PREFIX + 'owner') is None
        db._execute_write(lambda c: prune_retired_tombstones(c, older_than=0))
        assert all(db.get_meta(RETIRED_PREFIX + sid) for sid in ['owner', 'child'])


def test_sweep_characterization_keeps_permanent_and_expires_ordinary_fence(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        for sid in ['ordinary', 'permanent']:
            ended(db, sid)
        db.set_meta(RETIRED_PREFIX + 'permanent', json.dumps({'retired_at': 1}))
        db.save_gateway_routing_entry('route:ordinary', '{"session_id":"ordinary"}')
        db.save_gateway_routing_entry('route:kept', '{"session_id":"kept"}')
        db.set_meta('gateway.mutation.v1.fixture', json.dumps({
            'result': {'session_id': 'ordinary', 'title': 'private', 'summary': 'private', 'revision': 3}}))
        assert db.prune_sessions(older_than_days=None) == 2
        assert 'route:ordinary' not in db.load_gateway_routing_entries()
        assert 'route:kept' in db.load_gateway_routing_entries()
        receipt = json.loads(db.get_meta('gateway.mutation.v1.fixture'))
        assert receipt['result'] == {'session_id': 'ordinary', 'title': None, 'summary': None, 'revision': 3}
        db._execute_write(lambda c: prune_retired_tombstones(c, older_than=0))
        assert db.get_meta(RETIRED_PREFIX + 'ordinary') is None
        assert db.get_meta(RETIRED_PREFIX + 'permanent') is not None


def seed_many(db, count):
    def write(conn):
        conn.executemany("INSERT INTO sessions(id,source,started_at,last_activity_at,ended_at,message_count) "
                         "VALUES(?,'cron',1,1,2,1)", [(f's{n:04}',) for n in range(count)])
        conn.execute("INSERT INTO messages(session_id,role,content,timestamp) SELECT id,'tool','text',1 FROM sessions")
    db._execute_write(write)


def audit_deletions(db, monkeypatch, cap):
    """Enforce the transaction contract at the SQLite boundary, not from reported counts."""
    db._conn.execute('CREATE TEMP TABLE deletion_audit(session_id TEXT)')
    db._conn.execute("""CREATE TEMP TRIGGER audit_message_delete BEFORE DELETE ON messages BEGIN
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM state_meta
            WHERE key='gateway.retired_session.v1.' || OLD.session_id)
            THEN RAISE(ABORT, 'missing retirement fence') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sessions WHERE id=OLD.session_id)
            THEN RAISE(ABORT, 'session row deleted before messages') END;
        INSERT INTO deletion_audit VALUES(OLD.session_id);
    END""")
    original = db._execute_write
    counts = []
    def write(fn, *args, **kwargs):
        def measured(conn):
            conn.execute('DELETE FROM deletion_audit')
            result = fn(conn)
            count = conn.execute('SELECT COUNT(*) FROM deletion_audit').fetchone()[0]
            assert count <= cap
            counts.append(count)
            return result
        return original(measured, *args, **kwargs)
    monkeypatch.setattr(db, '_execute_write', write)
    return counts


def test_small_batches_fence_all_200_sessions_and_yield_to_another_writer(tmp_path, monkeypatch):
    import sqlite3
    with SessionDB(tmp_path / 'state.db') as db:
        seed_many(db, 200)
        counts = audit_deletions(db, monkeypatch, 7)
        deleted = set()
        while True:
            result = db.prune_sessions(older_than_days=None, message_cap=7)
            assert 0 < result['messages_deleted'] <= 7
            deleted.update(result['deleted_ids'])
            assert all(db.get_meta(RETIRED_PREFIX + sid) for sid in result['deleted_ids'])
            # A second connection takes the writer lock after every batch.
            with closing(sqlite3.connect(db.db_path, timeout=2)) as other, other:
                other.execute("INSERT OR REPLACE INTO state_meta(key,value) VALUES('other-writer','ok')")
            if not result['has_more']:
                break
        assert len(deleted) == 200 and sum(counts) == 200
        assert db._read_one('SELECT COUNT(*) FROM sessions')[0] == 0


@pytest.mark.parametrize('kind', ['local', 'compression'])
def test_conversation_crossing_batch_boundary_is_not_split(tmp_path, kind):
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 'a', messages=2)
        if kind == 'local':
            local_group(db, ['b', 'c'])
        else:
            ended(db, 'b')
            ended(db, 'c', parent_session_id='b')
            db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id='b'")
        first = db.prune_sessions(older_than_days=None, message_cap=3)
        assert first['deleted_ids'] == ['a'] and first['remaining'] == 2
        second = db.prune_sessions(older_than_days=None, message_cap=3)
        assert set(second['deleted_ids']) == {'b', 'c'}
        assert not second['has_more']


@pytest.mark.parametrize('kind', ['single', 'local', 'compression'])
def test_oversized_group_fences_first_reads_spillover_per_chunk_and_resumes(tmp_path, monkeypatch, kind):
    import hermes_state_media as media
    from hermes_state_prune import PENDING_PREFIX
    from tools.tool_result_storage import _build_persisted_message
    path = tmp_path / 'state.db'
    ids = ['s'] if kind == 'single' else ['s', 't']
    with SessionDB(path) as db:
        if kind == 'local':
            local_group(db, ids, 'bot-chat:fixture')
        else:
            ended(db, 's')
            if kind == 'compression':
                ended(db, 't', parent_session_id='s')
                db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id='s'")
        for n in range(6):
            text = _build_persisted_message(preview='text', has_more=True, original_size=50000,
                                            file_path=str(tmp_path / 'cache' / 'spillover' / f'{n}.txt'))
            db.append_message(ids[-1], 'tool', text, timestamp=1)
        total = db._read_one('SELECT COUNT(*) FROM messages')[0]
        audit_deletions(db, monkeypatch, 3)
        first = db.prune_sessions(older_than_days=None, message_cap=3)
        assert first['deleted'] == 0 and first['messages_deleted'] == 3
        assert set(first['in_progress_ids']) == set(ids)
        assert all(db.get_session(sid) and db.get_meta(RETIRED_PREFIX + sid) for sid in ids)
        first_candidates = {json.loads(r[0]) for r in db._read_all(
            'SELECT value FROM state_meta WHERE key GLOB ?', (media.SPILLOVER_PREFIX + '*',))}
        assert first_candidates == {f'{n}.txt' for n in range(3 - len(ids))}
        # Even a horizon sweep after an interrupted drain must retain its write fence.
        db._execute_write(lambda c: prune_retired_tombstones(c, older_than=0))
        assert all(db.get_meta(RETIRED_PREFIX + sid) for sid in ids)
    # Closing and reopening is the crash boundary: the local policy has already gone.
    with SessionDB(path) as db:
        audit_deletions(db, monkeypatch, 3)
        remaining = db._read_one('SELECT COUNT(*) FROM messages')[0]
        assert remaining == total - 3
        while True:
            result = db.prune_sessions(older_than_days=None, message_cap=3)
            assert result['messages_deleted'] <= 3
            if not result['has_more']:
                break
        assert set(result['deleted_ids']) == set(ids)
        assert all(db.get_session(sid) is None for sid in ids)
        assert not db._read_all('SELECT key FROM state_meta WHERE key GLOB ?', (PENDING_PREFIX + '*',))
        names = {json.loads(r[0]) for r in db._read_all('SELECT value FROM state_meta WHERE key GLOB ?',
                                                       (media.SPILLOVER_PREFIX + '*',))}
        assert names == {f'{n}.txt' for n in range(6)}


def test_chunk_transaction_rolls_back_fences_candidates_and_deletes_together(tmp_path, monkeypatch):
    import hermes_state_media as media
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 's', messages=8)
        original = media.retire_spillover
        def crash(conn, *args, **kwargs):
            original(conn, *args, **kwargs)
            raise RuntimeError('crash before commit')
        monkeypatch.setattr(media, 'retire_spillover', crash)
        with pytest.raises(RuntimeError, match='crash before commit'):
            db.prune_sessions(older_than_days=None, message_cap=3)
        assert db.get_meta(RETIRED_PREFIX + 's') is None
        assert db._read_one('SELECT COUNT(*) FROM messages')[0] == 8
        monkeypatch.setattr(media, 'retire_spillover', original)
        result = db.prune_sessions(older_than_days=None, message_cap=3)
        assert result['messages_deleted'] == 3


def test_exact_ids_report_open_unknown_archived_and_preserve_unrequested(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        for sid in ['gone', 'unrequested', 'archived']:
            ended(db, sid)
        db._write_sql("UPDATE sessions SET archived=1 WHERE id='archived'")
        db.create_session('open', 'cron')
        result = db.prune_sessions(older_than_days=None, session_ids={'gone', 'open', 'unknown', 'archived'},
                                   archived=False, message_cap=4)
        assert result['deleted_ids'] == ['gone']
        assert set(result['skipped_ids']) == {'open', 'unknown', 'archived'}
        assert all(db.get_session(sid) is not None for sid in ['unrequested', 'open', 'archived'])
        result = db.prune_sessions(older_than_days=None, session_ids={'archived'})
        assert result['deleted_ids'] == ['archived']


@pytest.mark.parametrize('kind', ['local', 'compression'])
def test_exact_ids_cannot_cut_a_conversation(tmp_path, kind):
    with SessionDB(tmp_path / 'state.db') as db:
        if kind == 'local':
            local_group(db, ['root', 'tip'])
        else:
            ended(db, 'root')
            ended(db, 'tip', parent_session_id='root')
            db._write_sql("UPDATE sessions SET end_reason='compression' WHERE id='root'")
        result = db.prune_sessions(older_than_days=None, session_ids={'root'}, message_cap=3)
        assert result['deleted'] == 0 and result['skipped_ids'] == ['root']
        assert all(db.get_session(sid) for sid in ['root', 'tip'])


@pytest.mark.parametrize('status', ['registered', 'running', 'unknown'])
def test_live_or_unknown_work_is_skipped_without_aborting_the_batch(tmp_path, status):
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 'busy')
        ended(db, 'quiet')
        db._write_sql("INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
                      "VALUES('w','busy','compute',1,0,?,'d')", (status,))
        result = db.prune_sessions(older_than_days=None, message_cap=3)
        assert result['deleted_ids'] == ['quiet'] and result['skipped_ids'] == ['busy']
        assert not result['has_more'] and db.get_session('busy')


def test_crash_between_batches_leaves_remaining_rows_intact(tmp_path):
    path = tmp_path / 'state.db'
    with SessionDB(path) as db:
        seed_many(db, 10)
        first = db.prune_sessions(older_than_days=None, message_cap=3)
        assert first['deleted'] == 3
    with SessionDB(path) as db:
        assert all(db.get_meta(RETIRED_PREFIX + sid) for sid in first['deleted_ids'])
        assert db._read_one('SELECT COUNT(*) FROM messages')[0] == 7
        deleted = set(first['deleted_ids'])
        while True:
            result = db.prune_sessions(older_than_days=None, message_cap=3)
            deleted.update(result['deleted_ids'])
            if not result['has_more']:
                break
        assert len(deleted) == 10


def test_prompt_finish_rechecks_a_survivor_created_after_read(tmp_path, monkeypatch):
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 'gone')
        db.create_session('kept', 'cron', system_prompt='shared')
        hashes = db._execute_write(lambda c: [db._store_system_prompt(c, p) for p in ['private', 'racing']])
        db._write_sql('UPDATE sessions SET system_prompt_hash=? WHERE id=?', (hashes[0], 'gone'))
        db.prune_sessions(older_than_days=None, session_ids={'gone'}, message_cap=3)
        assert db._read_one('SELECT 1 FROM system_prompts WHERE hash=?', (hashes[0],))
        original = db._execute_write
        first = True
        def race(fn, *args, **kwargs):
            nonlocal first
            if first:
                first = False
                import sqlite3
                with closing(sqlite3.connect(db.db_path)) as writer, writer:
                    writer.execute("INSERT INTO sessions(id,source,started_at,system_prompt_hash) VALUES('racer','cron',1,?)",
                                   (hashes[1],))
            return original(fn, *args, **kwargs)
        monkeypatch.setattr(db, '_execute_write', race)
        assert db.finish_prune_sessions(prompt_batch_size=1)['system_prompts_deleted'] == 1
        assert db.get_session('racer')['system_prompt'] == 'racing'
        assert db.get_session('kept')['system_prompt'] == 'shared'
        assert not db._read_one('SELECT 1 FROM system_prompts WHERE hash=?', (hashes[0],))


def test_other_thread_saves_every_50ms_during_500_session_prune(tmp_path):
    import sqlite3
    import threading
    import time
    stop, ready = threading.Event(), threading.Event()
    waits, errors = [], []
    with SessionDB(tmp_path / 'state.db') as db:
        seed_many(db, 500)
        def writer():
            try:
                with closing(sqlite3.connect(db.db_path, timeout=2)) as conn:
                    while not stop.is_set():
                        start = time.monotonic()
                        conn.execute("INSERT OR REPLACE INTO state_meta(key,value) VALUES('writer','ok')")
                        conn.commit()
                        waits.append(time.monotonic() - start)
                        ready.set()
                        stop.wait(0.05)
            except Exception as exc:
                errors.append(exc)
                ready.set()
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            assert ready.wait(5)
            deleted = 0
            while True:
                result = db.prune_sessions(older_than_days=None, message_cap=5)
                deleted += result['deleted']
                if not result['has_more']:
                    break
        finally:
            stop.set()
            thread.join(timeout=5)
        assert not thread.is_alive() and not errors
        assert deleted == 500 and len(waits) >= 2
        assert max(waits) < 2


def test_batch_writer_does_not_scan_unrelated_policies_or_routes(tmp_path, monkeypatch):
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 's')
        def seed(conn):
            conn.executemany('INSERT INTO state_meta(key,value) VALUES(?,?)', [
                (POLICY_PREFIX + str(n), json.dumps({'entry': {'session_id': str(n)}, 'policy': 'x' * 1024}))
                for n in range(1000)])
            conn.executemany("INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES('',?,?,1)",
                             [(str(n), json.dumps({'session_id': str(n)})) for n in range(1000)])
        db._execute_write(seed)
        ticks = []
        original = db._execute_write
        def write(fn, *args, **kwargs):
            def measured(conn):
                conn.set_progress_handler(lambda: ticks.append(1) and 0, 1000)
                try:
                    return fn(conn)
                finally:
                    conn.set_progress_handler(None, 0)
            return original(measured, *args, **kwargs)
        monkeypatch.setattr(db, '_execute_write', write)
        result = db.prune_sessions(older_than_days=None, message_cap=1)
        assert result['deleted_ids'] == ['s']
        assert len(ticks) < 20, 'the writer walked unrelated policy or routing history'


def test_route_repointed_after_snapshot_is_not_deleted(tmp_path, monkeypatch):
    import sqlite3
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 's')
        db.save_gateway_routing_entry('route', '{"session_id":"s"}')
        db.create_session('kept', 'cron')
        original = db._execute_write
        def race(fn, *args, **kwargs):
            with closing(sqlite3.connect(db.db_path)) as conn, conn:
                conn.execute('UPDATE gateway_routing SET entry_json=? WHERE session_key=?',
                             ('{"session_id":"kept"}', 'route'))
            return original(fn, *args, **kwargs)
        monkeypatch.setattr(db, '_execute_write', race)
        assert db.prune_sessions(older_than_days=None, message_cap=1)['deleted_ids'] == ['s']
        assert json.loads(db.load_gateway_routing_entries()['route'])['session_id'] == 'kept'


@pytest.mark.parametrize('write', ['append', 'constructor', 'accounting', 'accounting_transaction',
                                  'auxiliary', 'worker', 'reopen', 'mutation'])
def test_fenced_partial_transcript_cannot_accept_new_writes(tmp_path, write):
    import hermes_state_runtime as rt
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        ended(db, 's', messages=5)
        result = db.prune_sessions(older_than_days=None, message_cap=2)
        assert result['in_progress_ids'] == ['s']
        operations = {
            'append': lambda: db.append_message('s', 'user', 'not in the archive'),
            'constructor': lambda: db.ensure_session('s', source='cron'),
            'accounting': lambda: db.update_token_counts('s', input_tokens=1, model='m'),
            'auxiliary': lambda: db.record_auxiliary_usage('s', task='vision', input_tokens=1, model='m'),
            'accounting_transaction': lambda: db._execute_write(
                lambda conn: db._update_token_counts_in_transaction(conn, 's', input_tokens=1, model='m')),
            'mutation': lambda: rt.mutate_runtime_session(db, epoch=epoch, principal_id='human', session_id='s',
                request_id='rename', expected_revision=0, operation='rename', payload={'title': 'not archived'}),
            'worker': lambda: rt.register_worker_execution(db, epoch=epoch, execution_id='new', session_id='s',
                                                            generation=0, kind='compute', adoption_secret='fixture'),
            'reopen': lambda: db.reopen_session('s'),
        }
        with pytest.raises(rt.RuntimeStoreError, match='not_found'):
            operations[write]()
        assert db.get_session('s')['ended_at'] is not None
        assert db._read_one('SELECT COUNT(*) FROM messages')[0] == 3
        while db.prune_sessions(older_than_days=None, message_cap=2,
                                exclude_active_write_guards=True)['has_more']:
            pass
        assert db.get_session('s') is None


def test_partial_prune_keeps_exact_terminal_retry_but_refuses_new_admissions(tmp_path):
    import hermes_state_runtime as rt
    from tests.hermes_state.test_ledger_delete_consistency import _settled_admission
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        ended(db, 's', messages=5)
        admission = _settled_admission(db, epoch, 's')
        db._write_sql("UPDATE sessions SET ended_at=2 WHERE id='s'")
        assert db.prune_sessions(older_than_days=None, message_cap=2)['in_progress_ids'] == ['s']
        retry = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                      request_id='done', payload={'text': 'x'})
        assert retry['admission_id'] == admission and retry['status'] == 'terminal'
        with pytest.raises(rt.RuntimeStoreError, match='not_found'):
            rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                   request_id='new', payload={'text': 'not archived'})


def test_admission_staged_before_first_chunk_is_refused_under_writer_lock(tmp_path, monkeypatch):
    import hermes_state_runtime as rt
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        ended(db, 's', messages=5)
        original = db._execute_write
        first = True
        def prune_before_write(fn, *args, **kwargs):
            nonlocal first
            if first:
                first = False
                assert db.prune_sessions(older_than_days=None, message_cap=2)['in_progress_ids'] == ['s']
            return original(fn, *args, **kwargs)
        monkeypatch.setattr(db, '_execute_write', prune_before_write)
        with pytest.raises(rt.RuntimeStoreError, match='not_found'):
            rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                                   request_id='new', payload={'text': 'not archived'})
        assert not db._read_all('SELECT * FROM session_admissions')
        assert db._read_one('SELECT COUNT(*) FROM messages')[0] == 3


@pytest.mark.parametrize('cap', [0, -1, True, 1.5])
def test_invalid_message_cap_cannot_delete(tmp_path, cap):
    with SessionDB(tmp_path / 'state.db') as db:
        ended(db, 's')
        with pytest.raises(ValueError, match='positive integer'):
            db.prune_sessions(older_than_days=None, message_cap=cap)
        assert db.get_session('s')
