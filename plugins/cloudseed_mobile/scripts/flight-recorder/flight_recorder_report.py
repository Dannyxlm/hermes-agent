#!/usr/bin/env python3
"""Nightly flight-recorder triage for Hermex (round 9 U37; flight recorder plan U6, R10-R11).

Reads one UTC day of the owner-private flight recorder (``events/<day>.jsonl`` and
``flags/<day>/*/flag.json``) plus the server's own logs, and writes a Markdown report:

- error signatures, grouped (an error, a failed RPC or a failed REST call);
- smoothness spans against the round 9 Success Criteria budgets, by budget key;
- every flag, joined to server WARNING/ERROR lines within two minutes;
- the phone's server route timings from ``route_metrics`` (client label ``phone``);
- proposed fix items: any signature seen twice or more, any flag, any span key over budget.

A window that no server log covers reports ``unknown``, never zero. Read-only: it opens the
recorder and the logs for reading and writes only the report. Scheduled by R9-REL after
activation, e.g. ``flight_recorder_report.py --out-dir <CloudSeed>/outputs/hermex-flight-recorder``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

JOIN_WINDOW_S = 120
REPEAT_THRESHOLD = 2
# Span name -> (Success Criteria key, p90 budget in ms). Durations only; ScrollInteraction's B10
# budget is a hitch ratio (ms of hitch per s), which a duration span cannot judge.
BUDGETS = {
    'LaunchInboxContent': ('B2', 300),
    'ResumeInboxContent': ('B4', 250),
    'ChatOpenContent': ('B5', 150),
    'ComposerFocusToSettled': ('B7', 450),
    'TabFirstContent': ('B9', 150),
    'ScrollInteraction': ('B10', None),
}
TIMED_OUTCOMES = frozenset({'ok'})
DEFAULT_LOGS = ('gui.log', 'errors.log', 'agent.log')
_LOG_LINE = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) ([A-Z]+) (?:\[[^\]]*\] )?([\w.\-]+): ?(.*)')
_SERIOUS = frozenset({'WARNING', 'ERROR', 'CRITICAL'})


# ------------------------------------------------------------------ inputs

def load_day(root: Path, day: str):
    """``(batches, flags, unreadable_lines)`` for one UTC day of the recorder."""
    batches, unreadable = [], 0
    path = root / 'events' / f'{day}.jsonl'
    if path.is_file() and not path.is_symlink():
        with open(path, 'rb') as handle:
            for line in handle:
                try:
                    batches.append(json.loads(line))
                except ValueError:
                    unreadable += 1
    flags = []
    folder = root / 'flags' / day
    if folder.is_dir() and not folder.is_symlink():
        for entry in sorted(folder.iterdir()):
            record = entry / 'flag.json'
            if entry.is_dir() and not entry.is_symlink() and record.is_file():
                try:
                    flags.append(json.loads(record.read_text()) | {'_dir': str(entry)})
                except ValueError:
                    unreadable += 1
    return batches, flags, unreadable


def parse_log_time(stamp: str, millis: str) -> float:
    """Server logs carry the box's local wall time."""
    return dt.datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').timestamp() + int(millis) / 1000


def load_logs(paths):
    """``[(path, first_ts, last_ts, [(ts, level, logger, message)])]`` for readable logs."""
    logs = []
    for path in paths:
        try:
            handle = open(path, encoding='utf-8', errors='replace')
        except OSError:
            continue
        lines, first, last = [], None, None
        with handle:
            for raw in handle:
                match = _LOG_LINE.match(raw)
                if not match:
                    continue
                ts = parse_log_time(match[1], match[2])
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
                lines.append((ts, match[3], match[4], match[5].rstrip('\n')))
        if first is not None:
            logs.append((str(path), first, last, lines))
    return logs


# ---------------------------------------------------------------- analysis

def _signature_text(message: str) -> str:
    """A log message reduced to its shape: quoted text and numbers removed, bounded."""
    shape = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", message)
    shape = re.sub(r'[0-9a-f]{8,}|\d+', 'N', shape)
    return shape[:100]


def signatures(batches):
    """Failure signatures -> {count, installs, first, last}."""
    groups = defaultdict(lambda: {'count': 0, 'installs': set(), 'first': None, 'last': None})
    for batch in batches:
        for event in batch.get('events', ()):
            kind = event.get('kind')
            if kind == 'error':
                key = ('error', event.get('domain'), event.get('code'), event.get('context') or '-',
                       event.get('screen') or '-')
            elif kind == 'rpc' and event.get('outcome') in ('error', 'timeout'):
                key = ('rpc', event.get('method'), event.get('outcome'), event.get('code', '-'))
            elif kind == 'http' and event.get('outcome') in ('error', 'timeout'):
                key = ('http', event.get('verb'), event.get('route'), event.get('outcome'), event.get('status', '-'))
            else:
                continue
            group = groups[key]
            group['count'] += 1
            group['installs'].add(batch.get('install_id'))
            t = event.get('t')
            group['first'] = t if group['first'] is None else min(group['first'], t)
            group['last'] = t if group['last'] is None else max(group['last'], t)
    return dict(groups)


def _nearest_rank(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def span_budgets(batches):
    """Per span name: timed count, p50/p90/max, budget, verdict and other outcomes."""
    timed, others = defaultdict(list), defaultdict(Counter)
    for batch in batches:
        for event in batch.get('events', ()):
            if event.get('kind') != 'span':
                continue
            if event.get('outcome') in TIMED_OUTCOMES:
                timed[event.get('name')].append(event.get('ms', 0))
            else:
                others[event.get('name')][event.get('outcome')] += 1
    rows = {}
    for name, (key, budget) in BUDGETS.items():
        values = timed.get(name, [])
        row = {'key': key, 'budget_ms': budget, 'count': len(values), 'other_outcomes': dict(others.get(name, {}))}
        if values:
            row.update(p50=_nearest_rank(values, .5), p90=_nearest_rank(values, .9), max=max(values),
                       over_budget=sum(v > budget for v in values) if budget is not None else None)
            row['verdict'] = ('not gated' if budget is None else 'over' if row['p90'] > budget else 'within')
        else:
            row['verdict'] = 'no data'
        rows[name] = row
    return rows


def join_flag(flag, logs, window=JOIN_WINDOW_S):
    """Server WARNING/ERROR shapes within ``window`` s of a flag, or None when no log covers it."""
    t = flag.get('t', 0) / 1000
    start, end = t - window, t + window
    covering = [log for log in logs if log[1] <= start and log[2] >= end]
    if not covering:
        return None
    found = Counter()
    for _path, _first, _last, lines in covering:
        for ts, level, logger, message in lines:
            if start <= ts <= end and level in _SERIOUS:
                found[(level, logger, _signature_text(message))] += 1
    return found


def phone_routes(logs, day_start, day_end):
    """Worst p95/p99 per phone route from route_metrics summaries in the day, or None."""
    rows, seen = {}, False
    for _path, _first, _last, lines in logs:
        for ts, _level, logger, message in lines:
            if not logger.endswith('route_metrics') or not day_start <= ts < day_end:
                continue
            try:
                summary = json.loads(message.split('route_metrics ', 1)[1])
            except (IndexError, ValueError):
                continue
            seen = True
            for item in summary:
                label = item.get('label') or []
                if len(label) < 5 or label[2] != 'phone':
                    continue
                key = (label[0], label[1], label[4])
                row = rows.setdefault(key, {'count': 0, 'p95_ms': 0, 'p99_ms': 0})
                row['count'] += item.get('count', 0)
                for pct in ('p95_ms', 'p99_ms'):
                    row[pct] = _worse(row[pct], item.get(pct))
    return rows if seen else None


def _worse(current, value):
    """Bucket bounds compare numerically; the explicit overflow marker beats every bound."""
    if 'overflow' in (current, value):
        return 'overflow'
    return max(current, value) if isinstance(value, (int, float)) else current


# ------------------------------------------------------------------ report

def _utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime('%H:%M:%S') if ms else '-'


def build_report(root: Path, day: str, log_paths) -> str:
    batches, flags, unreadable = load_day(root, day)
    logs = load_logs(log_paths)
    day_start = dt.datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=dt.timezone.utc).timestamp()
    sigs = signatures(batches)
    spans = span_budgets(batches)
    events = sum(len(b.get('events', ())) for b in batches)
    installs = {b.get('install_id') for b in batches}
    builds = sorted({b.get('app_build') for b in batches if b.get('app_build')})
    out = [f'# Hermex flight recorder: {day} (UTC)', '',
           f'- Batches {len(batches)}, events {events}, installations {len(installs)}, flags {len(flags)}.',
           f'- App builds: {", ".join(builds) if builds else "none"}.',
           f'- Server logs read: {", ".join(p for p, *_ in logs) if logs else "none (joins are unknown)"}.']
    if unreadable:
        out.append(f'- Unreadable stored lines: {unreadable}.')
    out += ['', '## Error signatures', '']
    if sigs:
        out += ['| signature | count | installs | first | last |', '|---|---|---|---|---|']
        for key, group in sorted(sigs.items(), key=lambda item: (-item[1]['count'], str(item[0]))):
            out.append(f"| `{' · '.join(str(part) for part in key)}` | {group['count']} | {len(group['installs'])} "
                       f"| {_utc(group['first'])} | {_utc(group['last'])} |")
    else:
        out.append('None recorded.')
    out += ['', '## Smoothness spans against the budgets', '',
            '| span | key | budget p90 | count | p50 | p90 | max | over budget | verdict | other outcomes |',
            '|---|---|---|---|---|---|---|---|---|---|']
    for name, row in spans.items():
        budget = f"{row['budget_ms']} ms" if row['budget_ms'] is not None else 'hitch ratio'
        others = ', '.join(f'{k} {v}' for k, v in sorted(row['other_outcomes'].items())) or '-'
        out.append(f"| {name} | {row['key']} | {budget} | {row['count']} | {row.get('p50', '-')} | "
                   f"{row.get('p90', '-')} | {row.get('max', '-')} | {row.get('over_budget', '-')} | "
                   f"{row['verdict']} | {others} |")
    out += ['', '## Flags', '']
    joins = []
    if not flags:
        out.append('None sent.')
    for flag in flags:
        found = join_flag(flag, logs)
        joins.append(found)
        note = ' '.join(str(flag.get('note', '')).split())[:280]
        out.append(f"- {_utc(flag.get('t'))} on `{flag.get('screen')}`, build {flag.get('app_build')}, "
                   f"screenshot {'yes' if flag.get('has_screenshot') else 'no'}: {note or '(no note)'}  ")
        out.append(f"  `{flag.get('_dir')}`")
        if found is None:
            out.append(f'  - Server log lines within {JOIN_WINDOW_S // 60} min: unknown (no log covers this window).')
        elif not found:
            out.append(f'  - Server log lines within {JOIN_WINDOW_S // 60} min: 0 WARNING/ERROR.')
        else:
            for (level, logger, shape), count in found.most_common(10):
                out.append(f'  - {count} × {level} {logger}: {shape}')
    out += ['', '## Phone server route timings (route_metrics)', '']
    routes = phone_routes(logs, day_start, day_start + 86400)
    if routes is None:
        out.append('unknown (no route_metrics summary in the logs for this day).')
    elif not routes:
        out.append('No phone-labelled rows.')
    else:
        out += ['| route | status | count | worst p95 | worst p99 |', '|---|---|---|---|---|']
        for (kind, template, status), row in sorted(routes.items(), key=lambda item: -item[1]['count'])[:25]:
            out.append(f"| {kind} {template} | {status} | {row['count']} | {row['p95_ms']} | {row['p99_ms']} |")
    proposals = [f"Fix signature `{' · '.join(str(p) for p in key)}` ({group['count']}×)"
                 for key, group in sigs.items() if group['count'] >= REPEAT_THRESHOLD]
    proposals += [f"Investigate flag at {_utc(flag.get('t'))} on `{flag.get('screen')}`" for flag in flags]
    proposals += [f"{name} ({row['key']}) p90 {row['p90']} ms is over its {row['budget_ms']} ms budget"
                  for name, row in spans.items() if row['verdict'] == 'over']
    out += ['', '## Proposed items', '']
    out += [f'- {item}' for item in proposals] or ['None.']
    return '\n'.join(out) + '\n'


def _hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, help='flight recorder root (default: <Hermes root>/mobile/flight-recorder)')
    parser.add_argument('--day', help='UTC day YYYY-MM-DD (default: yesterday)')
    parser.add_argument('--log', action='append', dest='logs', type=Path,
                        help='server log to join (repeatable; default: gui.log, errors.log, agent.log)')
    parser.add_argument('--out-dir', type=Path, help='write <day>.md here (0600); default prints to stdout')
    args = parser.parse_args(argv)
    root = args.root or _hermes_root() / 'mobile' / 'flight-recorder'
    day = args.day or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime('%Y-%m-%d')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        parser.error('--day must be YYYY-MM-DD')
    logs = args.logs or [_hermes_root() / 'logs' / name for name in DEFAULT_LOGS]
    report = build_report(root, day, logs)
    if args.out_dir is None:
        sys.stdout.write(report)
        return 0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    target = args.out_dir / f'{day}.md'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(report)
    print(target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
