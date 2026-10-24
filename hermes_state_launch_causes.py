"""Typed terminal causes derived from execution receipts, never from activity descriptions."""
import json
import time

from hermes_state_launches import TERMINAL_CAUSES

GATEWAY_SHUTDOWN = 'Gateway shutting down'
GATEWAY_RESTART = 'Gateway restarting'
RESTART_END_REASONS = frozenset({'ws_orphan_reap', 'startup_orphan_reap', 'owner_restart'})
CONTINUATION_END_REASONS = frozenset({'compression', 'compression_in_place'})


def result_cause(result, *, stop_cause=None, outcome=None):
    value = (result or {}).get('result', result or {})
    if value.get('interrupted') or outcome == 'interrupted':
        recorded = value.get('terminal_cause') or stop_cause
        if recorded in TERMINAL_CAUSES:
            return recorded
        return {GATEWAY_RESTART: 'owner_restart', GATEWAY_SHUTDOWN: 'parent_stop'}.get(
            value.get('interrupt_message'), 'unknown')
    if value.get('failed') or value.get('error') or outcome in {'failed', 'rejected'}:
        return 'error'
    if value.get('completed') is True or outcome == 'completed':
        return 'success'
    if value.get('completed') is False:
        return 'error'
    return 'unknown'


def finish_for_session(conn, session_id, terminal_cause):
    from hermes_state_runtime_workers import runtime_lineage
    if conn.execute('SELECT 1 FROM work_launches LIMIT 1').fetchone() is None:
        return
    ids = runtime_lineage(conn, session_id)
    conn.execute("UPDATE work_launches SET finished_at=COALESCE(finished_at,?),terminal_cause=? WHERE "
                 "(finished_at IS NULL OR (terminal_cause='unknown' AND ? != 'unknown')) "
                 'AND child_session_id IN (SELECT value FROM json_each(?))',
                 (time.time(), terminal_cause, terminal_cause, json.dumps(sorted(ids))))


def finish_on_end(conn, session_id, end_reason):
    if end_reason in CONTINUATION_END_REASONS:
        return
    cause = ('owner_restart' if end_reason in RESTART_END_REASONS else
             {'user_stop': 'user_stop', 'parent_stop': 'parent_stop', 'cron_complete': 'success',
              'cron_incomplete_no_output': 'error'}.get(end_reason, 'unknown'))
    finish_for_session(conn, session_id, cause)


def stamp_stop(agent, cause):
    pending = vars(agent).get("_hard_interrupt_requested")
    if pending is None or not pending.is_set():
        agent._launch_stop_cause = cause


def finish_child(child, *, result=None, cause=None):
    lid, db = getattr(child, '_launch_id', None), getattr(child, '_session_db', None)
    if isinstance(lid, str) and lid and db is not None:
        from agent.runtime_session_store import call_off_event_loop
        call_off_event_loop(db.finish_launch, lid, cause or result_cause(
            result, stop_cause=getattr(child, '_launch_stop_cause', None)))


def process_cause(session):
    if session.completion_reason == 'failed_start' or session.termination_source == 'failed_start':
        return 'failed_start'
    if session.completion_reason == 'killed':
        if session.termination_source == 'gateway_restart':
            return 'owner_restart'
        if session.termination_source in {'process.kill', 'process.stop', 'slash.stop', 'cli.stop'}:
            return 'user_stop'
        return 'parent_stop'
    if session.completion_reason == 'lost' or (session.completion_reason == 'already_exited' and session.exit_code is None):
        return 'unknown'
    return ('success' if session.exit_code == 0 else 'error') if session.exit_code is not None else 'unknown'
