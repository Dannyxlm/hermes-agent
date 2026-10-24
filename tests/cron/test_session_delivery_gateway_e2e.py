"""Exact-session cron wakes through an ordinary disposable gateway and loopback model."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

import pytest

from tests.gateway.test_normal_runtime_boot import control
from tests.waiting import wait_until


@pytest.mark.platforms('linux')
def test_exact_session_wakes_idle_busy_cold_and_rotated(tmp_path, monkeypatch, model_peer):
    from cron.bot_chat_legacy import drain_legacy_pending, read_pending
    from cron.session_delivery import deliver_to_session
    from hermes_cli.gateway_client import connect_gateway
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from hermes_state import SessionDB

    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HOME', str(user))
    model_url = f'http://127.0.0.1:{model_peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'local-wire-stub', 'base_url': model_url},
        'auxiliary': {'title_generation': {'enabled': False}},
    }))
    model_peer.blocked, model_peer.release = threading.Event(), threading.Event()
    with SessionDB(home / 'state.db') as db:
        for sid in ('cold', 'idle', 'busy', 'rotated'):
            db.create_session(sid, 'cli')
        db.append_message('rotated', 'user', 'synthetic history')
        assert db.try_acquire_compression_lock('rotated', 'fixture-compression')
        db.publish_compression_child(parent_session_id='rotated', child_session_id='rotated-child',
            source='cli', messages=[{'role': 'user', 'content': 'synthetic summary'}],
            compression_lock_holder='fixture-compression')

    def deliver(sid):
        job = {'id': sid, 'name': 'synthetic wake', 'execution_id': 'fixture-' + sid}
        deliver_to_session(job, 'synthetic scheduled result', sid)
        return job['_bot_chat_delivery_receipts']['session:' + sid]['delivery_id']

    cold_key = deliver('cold')
    assert read_pending(cold_key)['status'] == 'queued'
    drain_legacy_pending()  # not ready: this tick must not claim or lose the record
    assert read_pending(cold_key)['status'] == 'queued'
    assert 'admission_id' not in read_pending(cold_key)
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
               TMPDIR=str(tmp_path.parent.parent),
               PYTHONPATH=str(root), PYTHONUNBUFFERED='1',
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=model_url)

    def ready():
        try:
            descriptor = control(home, 'identify')
            return descriptor if descriptor.get('state') == 'ready' else None
        except (OSError, ValueError):
            return None

    def settled(key):
        drain_legacy_pending()
        return read_pending(key)['status'] == 'settled'

    async def rpc(method, **params):
        endpoint = discover_gateway_endpoint(home, timeout=5).endpoint
        assert endpoint is not None
        async with connect_gateway(endpoint=endpoint) as client:
            if method != 'session.resume' and 'session_id' in params:
                await client.rpc('session.resume', session_id=params['session_id'],
                                 omit_messages=True, defer_history=True)
            return await client.rpc(method, **params)

    with (tmp_path / 'gateway.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=root, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            descriptor = wait_until(ready, timeout=45, what='disposable gateway readiness')
            assert descriptor['api_origin'].startswith('http://127.0.0.1:')
            pointer = home / 'gateway.sock.path'
            control_path = Path(pointer.read_text().strip()) if pointer.exists() else home / 'gateway.sock'
            assert control_path.is_relative_to(tmp_path.parent.parent)
            wait_until(lambda: settled(cold_key), timeout=45, what='next-tick cold wake completion')
            idle_key = deliver('idle')
            wait_until(lambda: settled(idle_key), timeout=45, what='idle wake completion')
            busy_snapshot = asyncio.run(rpc('session.resume', session_id='busy'))
            running = asyncio.run(rpc('prompt.submit', session_id=busy_snapshot['session_id'],
                submission_id='fixture-running', text='BLOCK_FIFO', queued=True, finite=True))
            assert model_peer.blocked.wait(15)
            busy_key = deliver('busy')
            queued = read_pending(busy_key)
            assert queued['receipt_status'] == 'queued'
            queued_receipt = asyncio.run(rpc('prompt.receipt', session_id=busy_snapshot['session_id'],
                                            admission_id=queued['admission_id']))
            assert queued_receipt['status'] == 'queued'
            first_receipt = asyncio.run(rpc('prompt.receipt', session_id=busy_snapshot['session_id'],
                                           admission_id=running['admission_id']))
            assert first_receipt['status'] == 'started'
            model_peer.release.set()
            wait_until(lambda: settled(busy_key), timeout=45, what='FIFO wake completion')
            final_running = asyncio.run(rpc('prompt.receipt', session_id=busy_snapshot['session_id'],
                                           admission_id=running['admission_id']))
            assert final_running['status'] == 'terminal' and final_running['outcome'] == 'completed'
            # Distinct physical child, but compare the authority's canonical snapshot/receipt refs.
            rotated_snapshot = asyncio.run(rpc('session.resume', session_id='rotated'))
            rotated_key = deliver('rotated')
            wait_until(lambda: settled(rotated_key), timeout=45, what='rotated wake completion')
            rotated_record = read_pending(rotated_key)
            receipt = asyncio.run(rpc('prompt.receipt', session_id=rotated_snapshot['session_id'],
                                     admission_id=rotated_record['admission_id']))
            assert receipt['ref']['session_id'] == rotated_snapshot['session_id']
            assert rotated_record['target_session_id'] == rotated_snapshot['session_id']
            # A drain after settlement must never infer again.
            before = len(model_peer.requests)
            last_users = [next((m.get('content', '') for m in reversed(r.get('messages', []))
                                if m.get('role') == 'user'), '') for r in model_peer.requests]
            assert 'BLOCK_FIFO' in json.dumps(last_users[2])
            assert '[Scheduled wake from cron job' in json.dumps(last_users[3])
            drain_legacy_pending()
            assert len(model_peer.requests) == before == 5
            assert all(read_pending(key)['status'] == 'settled'
                       for key in (cold_key, idle_key, busy_key, rotated_key))
            process.send_signal(signal.SIGINT)
            assert process.wait(timeout=25) == 0
            evidence = {'idle_completed': True, 'busy_fifo_completed': True,
                'not_ready_stayed_queued': True, 'next_tick_completed': True,
                'rotated_receipt_matches_snapshot': True, 'no_replay': True,
                'loopback_only': True, 'model_requests': before, 'clean_gateway_exit': True}
            (tmp_path / 'cron-e2e-receipt.json').write_text(json.dumps(evidence))
            print(json.dumps(evidence))
        finally:
            model_peer.release.set()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
