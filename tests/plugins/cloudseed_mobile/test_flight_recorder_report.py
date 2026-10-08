"""Nightly flight-recorder triage report (round 9 U37). Synthetic recorder data and logs only."""
import datetime as dt
import importlib.util
import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from plugins.cloudseed_mobile import flight_recorder as fr

SCRIPT = Path(__file__).resolve().parents[3] / 'plugins/cloudseed_mobile/scripts/flight-recorder/flight_recorder_report.py'
DAY = '2026-10-09'
T = int(dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
INSTALL = str(uuid.UUID('7a1c0de5-0000-4000-8000-00000000abcd'))


@pytest.fixture(scope='module')
def report():
    spec = importlib.util.spec_from_file_location('flight_recorder_report', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def store(root, events, seq=0, flags=()):
    """Write through the real ingest so the report reads exactly what the server stores."""
    recorder = fr.FlightRecorderStore(root, now=lambda: dt.datetime(2026, 10, 9, 13, 0, tzinfo=dt.timezone.utc))
    body = {'profile': 'default', 'schema': 1, 'install_id': INSTALL, 'batch_seq': seq, 'app_build': '2026100801',
            'os_version': '27.0', 'device_class': 'iPhone18,1', 'events': events}
    recorder.put_batch('default', fr.validate_batch(body))
    for flag_t, note in flags:
        recorder.put_flag('default', fr.validate_flag({
            'profile': 'default', 'schema': 1, 'install_id': INSTALL, 'flag_id': str(uuid.uuid4()), 't': flag_t,
            'screen': 'chat', 'note': note, 'app_build': '2026100801', 'os_version': '27.0',
            'device_class': 'iPhone18,1', 'events': []}))


def log_line(ts_ms, level, logger, message):
    local = dt.datetime.fromtimestamp(ts_ms / 1000)
    return f"{local:%Y-%m-%d %H:%M:%S},{ts_ms % 1000:03d} {level} {logger}: {message}\n"


def test_budget_keys_cover_every_span_name(report):
    assert set(report.BUDGETS) == fr.SPAN_NAMES


def test_two_signatures_group_separately(report, tmp_path):
    events = [
        {'kind': 'error', 't': T, 'domain': 'url', 'code': -1001, 'screen': 'inbox'},
        {'kind': 'error', 't': T + 5, 'domain': 'url', 'code': -1001, 'screen': 'inbox'},
        {'kind': 'error', 't': T + 9, 'domain': 'url', 'code': -1005, 'screen': 'inbox'},
        {'kind': 'rpc', 't': T + 10, 'method': 'session.list', 'outcome': 'timeout', 'ms': 15000},
        {'kind': 'rpc', 't': T + 11, 'method': 'session.list', 'outcome': 'ok', 'ms': 40},
    ]
    store(tmp_path, events)
    batches, _flags, _bad = report.load_day(tmp_path, DAY)
    sigs = report.signatures(batches)
    assert sigs[('error', 'url', -1001, '-', 'inbox')]['count'] == 2
    assert sigs[('error', 'url', -1005, '-', 'inbox')]['count'] == 1
    assert sigs[('rpc', 'session.list', 'timeout', '-')]['count'] == 1
    assert len(sigs) == 3
    text = report.build_report(tmp_path, DAY, [])
    assert 'Fix signature `error · url · -1001 · - · inbox` (2×)' in text
    assert '-1005' in text.split('## Proposed items')[0] and '-1005' not in text.split('## Proposed items')[1]


def test_slow_span_is_flagged_against_its_budget(report, tmp_path):
    events = [{'kind': 'span', 't': T + i, 'name': 'ChatOpenContent', 'outcome': 'ok', 'ms': ms}
              for i, ms in enumerate([90, 100, 110, 120, 130, 140, 145, 148, 400, 900])]
    events += [{'kind': 'span', 't': T + 50 + i, 'name': 'LaunchInboxContent', 'outcome': 'ok', 'ms': 200}
               for i in range(5)]
    events.append({'kind': 'span', 't': T + 60, 'name': 'ChatOpenContent', 'outcome': 'cancelled', 'ms': 5})
    store(tmp_path, events)
    rows = report.span_budgets(report.load_day(tmp_path, DAY)[0])
    chat = rows['ChatOpenContent']
    assert (chat['key'], chat['budget_ms'], chat['count'], chat['p90'], chat['over_budget']) == ('B5', 150, 10, 400, 2)
    assert chat['verdict'] == 'over' and chat['other_outcomes'] == {'cancelled': 1}
    assert rows['LaunchInboxContent']['verdict'] == 'within'
    assert rows['TabFirstContent']['verdict'] == 'no data'
    text = report.build_report(tmp_path, DAY, [])
    assert 'ChatOpenContent (B5) p90 400 ms is over its 150 ms budget' in text
    assert 'LaunchInboxContent (B2) p90' not in text


def test_flag_joins_server_log_lines_within_two_minutes(report, tmp_path):
    store(tmp_path, [{'kind': 'lifecycle', 't': T, 'state': 'foreground'}], flags=[(T, 'the composer jumped')])
    log = tmp_path / 'gui.log'
    log.write_text(''.join([
        log_line(T - 600_000, 'INFO', 'hermes_cli.web_server', 'boot'),
        log_line(T - 60_000, 'ERROR', 'tui_gateway.ws', 'send failed for session 20261009_115900_abcd1234'),
        log_line(T + 90_000, 'WARNING', 'tui_gateway.ws', 'send failed for session 20261009_120130_beef5678'),
        log_line(T + 30_000, 'INFO', 'tui_gateway.ws', 'routine'),
        log_line(T + 200_000, 'ERROR', 'tui_gateway.ws', 'too late'),
        log_line(T + 600_000, 'INFO', 'hermes_cli.web_server', 'later'),
    ]))
    _batches, flags, _bad = report.load_day(tmp_path, DAY)
    found = report.join_flag(flags[0], report.load_logs([log]))
    assert sum(found.values()) == 2
    assert {level for level, _logger, _shape in found} == {'ERROR', 'WARNING'}
    assert all('20261009' not in shape for _level, _logger, shape in found)
    text = report.build_report(tmp_path, DAY, [log])
    assert 'the composer jumped' in text and 'Investigate flag at 12:00:00' in text
    assert 'too late' not in text and 'routine' not in text


def test_missing_or_uncovering_logs_report_unknown_not_zero(report, tmp_path):
    store(tmp_path, [{'kind': 'lifecycle', 't': T, 'state': 'foreground'}], flags=[(T, 'stuck')])
    _batches, flags, _bad = report.load_day(tmp_path, DAY)
    assert report.join_flag(flags[0], report.load_logs([tmp_path / 'missing.log'])) is None
    short = tmp_path / 'short.log'
    short.write_text(log_line(T - 30_000, 'INFO', 'x', 'a') + log_line(T + 30_000, 'INFO', 'x', 'b'))
    assert report.join_flag(flags[0], report.load_logs([short])) is None
    quiet = tmp_path / 'quiet.log'
    quiet.write_text(log_line(T - 300_000, 'INFO', 'x', 'a') + log_line(T + 300_000, 'INFO', 'x', 'b'))
    assert report.join_flag(flags[0], report.load_logs([quiet])) == {}

    unknown = report.build_report(tmp_path, DAY, [tmp_path / 'missing.log'])
    assert 'Server log lines within 2 min: unknown' in unknown
    assert 'route_metrics' in unknown and 'unknown (no route_metrics summary' in unknown
    assert 'Server log lines within 2 min: 0 WARNING/ERROR' in report.build_report(tmp_path, DAY, [quiet])


def test_phone_route_timings_come_from_route_metrics(report, tmp_path):
    rows = [{'label': ['http', '/api/sessions', 'phone', '2026100801', '2xx'], 'count': 40, 'p95_ms': 250, 'p99_ms': 500},
            {'label': ['http', '/api/sessions', 'desktop', '0.0.0', '2xx'], 'count': 90, 'p95_ms': 2500, 'p99_ms': 5000},
            {'label': ['rpc', 'session.list', 'phone', '2026100801', 'success'], 'count': 3, 'p95_ms': 100,
             'p99_ms': 'overflow'}]
    log = tmp_path / 'gui.log'
    log.write_text(log_line(T, 'INFO', 'hermes_cli.web_server.route_metrics', 'route_metrics ' + json.dumps(rows)))
    store(tmp_path, [{'kind': 'lifecycle', 't': T, 'state': 'foreground'}])
    text = report.build_report(tmp_path, DAY, [log])
    assert '| http /api/sessions | 2xx | 40 | 250 | 500 |' in text
    assert '| rpc session.list | success | 3 | 100 | overflow |' in text
    assert '2500' not in text


def test_cli_writes_a_private_report_for_the_day(report, tmp_path):
    root = tmp_path / 'recorder'
    store(root, [{'kind': 'bg_refresh', 't': T, 'trigger': 'scheduled', 'outcome': 'new_data', 'ms': 900},
                 {'kind': 'push_tap', 't': T + 1, 'source': 'banner', 'outcome': 'opened'}])
    out = tmp_path / 'outputs'
    assert report.main(['--root', str(root), '--day', DAY, '--log', str(tmp_path / 'none.log'),
                        '--out-dir', str(out)]) == 0
    target = out / f'{DAY}.md'
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o600
    text = target.read_text()
    assert text.startswith(f'# Hermex flight recorder: {DAY} (UTC)')
    assert 'Batches 1, events 2, installations 1, flags 0.' in text
    with pytest.raises(SystemExit):
        report.main(['--root', str(root), '--day', '2026-10-09; rm -rf', '--out-dir', str(out)])
