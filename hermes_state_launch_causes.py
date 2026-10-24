"""Typed terminal causes derived from execution receipts, never from activity descriptions."""
from hermes_state_optional_launch import best_effort
import json
import time

from hermes_state_launches import TERMINAL_CAUSES

GATEWAY_SHUTDOWN = 'Gateway shutting down'
GATEWAY_RESTART = 'Gateway restarting'
RESTART_END_REASONS = frozenset({'startup_orphan_reap', 'owner_restart'})
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


@best_effort
def finish_for_session(conn, session_id, terminal_cause, *, turn_only=False):
    from hermes_state_runtime_workers import runtime_lineage
    if conn.execute('SELECT 1 FROM work_launches LIMIT 1').fetchone() is None:
        return
    ids = runtime_lineage(conn, session_id)
    conn.execute("UPDATE work_launches SET finished_at=COALESCE(finished_at,?),terminal_cause=? WHERE "
                 "(finished_at IS NULL OR (terminal_cause='unknown' AND ? != 'unknown')) "
                 'AND child_session_id IN (SELECT value FROM json_each(?)) '
                 "AND (?=0 OR kind NOT IN ('lane','teammate'))",
                 (time.time(), terminal_cause, terminal_cause, json.dumps(sorted(ids)), int(turn_only)))


@best_effort
def finish_on_end(conn, session_id, end_reason):
    if end_reason in CONTINUATION_END_REASONS:
        return
    cause = ('owner_restart' if end_reason in RESTART_END_REASONS else
             {'user_stop': 'user_stop', 'parent_stop': 'parent_stop', 'cron_complete': 'success',
              'cron_incomplete_no_output': 'error'}.get(end_reason, 'unknown'))
    if end_reason == 'cli_close':
        from hermes_state_terminal import RESULT_PREFIX, load_result
        from hermes_state_runtime_workers import runtime_lineage
        ids = runtime_lineage(conn, session_id)
        receipt = conn.execute(
            'SELECT a.status,a.outcome,m.value FROM session_admissions a LEFT JOIN state_meta m '
            'ON m.key=? || a.admission_id WHERE a.target_session_id IN (SELECT value FROM json_each(?)) '
            'ORDER BY a.seq DESC LIMIT 1', (RESULT_PREFIX, json.dumps(sorted(ids)))).fetchone()
        if receipt is not None and receipt['status'] == 'terminal':
            cause = result_cause(load_result(receipt['value']) if receipt['value'] else {}, outcome=receipt['outcome'])
    finish_for_session(conn, session_id, cause)


def stamp_stop(agent, cause):
    pending = vars(agent).get("_hard_interrupt_requested")
    if pending is None or not pending.is_set():
        agent._launch_stop_cause = cause


@best_effort
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
