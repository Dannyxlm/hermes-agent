#!/usr/bin/env python3
"""Content-free snapshot benchmark for the polled native read handlers.

Run base and candidate in separate processes with the SAME disposable --home and --now.
The source snapshot must be complete and quiescent. No live home/config/credentials are read.
Cold means application-memo invalidation, not dropping the host's filesystem page cache.
For an additive state_meta index, --prepare-lookups copies ONLY state_meta into private
sidecars; --lookups shadows that table via a TEMP view on each read connection. This
exercises the real query/index without copying or modifying the multi-GB message stores.
Only timings, counts, hashes and function-level profiles are written; never payloads.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import cProfile
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import pstats
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--now', type=float, required=True)
    parser.add_argument('--iterations', type=int, default=10)
    parser.add_argument('--lookups', type=Path)
    parser.add_argument('--prepare-lookups', action='store_true')
    parser.add_argument('--sweep', action='store_true')
    parser.add_argument('--profile', action='store_true')
    return parser.parse_args()


def sandbox(args):
    if args.home.exists() and not (args.home / '.read-probe-home').exists():
        raise ValueError('refusing a home not created by this probe')
    args.home.mkdir(parents=True, exist_ok=True)
    args.home.chmod(0o700)
    (args.home / '.read-probe-home').touch()
    home = args.home / '.hermes'
    home.mkdir(exist_ok=True)
    os.environ['HOME'] = str(args.home)
    os.environ['HERMES_HOME'] = str(home)
    os.environ['TERMINAL_CWD'] = str(args.home / 'repo')
    os.environ.pop('HERMES_TEST_ISOLATION', None)
    paths = sorted(args.snapshot.glob('*/state.db'))
    if not paths:
        raise ValueError('snapshot contains no profile stores')
    for source in paths:
        target = home if source.parent.name == 'default' else home / 'profiles' / source.parent.name
        target.mkdir(parents=True, exist_ok=True)
        link = target / 'state.db'
        if not link.exists():
            link.symlink_to(source.resolve())
        if not link.is_symlink() or link.resolve() != source.resolve():
            raise ValueError('unexpected sandbox state.db')
        (target / 'config.yaml').write_text('{}\n', encoding='utf-8')
    return home, paths


def prepare_lookups(args, paths):
    from hermes_state_common import SCHEMA_SQL
    with closing(sqlite3.connect(':memory:')) as schema:
        schema.executescript(SCHEMA_SQL)
        ddl = schema.execute("SELECT sql FROM sqlite_master WHERE name='idx_state_meta_local_target'").fetchone()[0]
    args.lookups.mkdir(parents=True, exist_ok=True)
    args.lookups.chmod(0o700)
    for source in paths:
        target = args.lookups / (source.parent.name + '.db')
        if target.exists():
            raise ValueError('refusing to overwrite a lookup sidecar')
        with closing(sqlite3.connect(target, uri=True)) as dest:
            dest.execute('ATTACH DATABASE ? AS snapshot', (source.resolve().as_uri() + '?mode=ro',))
            dest.execute('CREATE TABLE state_meta(key TEXT PRIMARY KEY,value TEXT)')
            dest.execute('INSERT INTO state_meta SELECT key,value FROM snapshot.state_meta')
            dest.execute(ddl)
            dest.commit()
            count = dest.execute('SELECT count(*) FROM state_meta').fetchone()[0]
        target.chmod(0o600)
        print(json.dumps({'prepared_profile': source.parent.name, 'meta_rows': count}), flush=True)


def readonly_connections(args, paths):
    connect = sqlite3.connect
    # Resolve symlinked homes before mapping, just as read_profile does.
    lookup = {str(p.resolve()): args.lookups / (p.parent.name + '.db') for p in paths} if args.lookups else {}

    def guarded(database, *positional, **kwargs):
        text = str(database)
        if 'state.db' in text and 'mode=ro' not in text:
            raise AssertionError('probe attempted a writable state.db connection')
        conn = connect(database, *positional, **kwargs)
        if text.startswith('file:') and 'state.db' in text:
            from urllib.parse import unquote, urlparse
            path = str(Path(unquote(urlparse(text).path)).resolve())
            sidecar = lookup.get(path)
            if sidecar is not None:
                conn.execute('ATTACH DATABASE ? AS probe_lookup', (sidecar.resolve().as_uri() + '?mode=ro',))
                conn.execute('CREATE TEMP VIEW state_meta AS SELECT * FROM probe_lookup.state_meta')
        return conn
    sqlite3.connect = guarded


def offline():
    real_getaddrinfo = socket.getaddrinfo

    def local_only(host, *args, **kwargs):
        if host not in ('localhost', '127.0.0.1', '::1', None):
            raise OSError('snapshot probe is offline')
        return real_getaddrinfo(host, *args, **kwargs)
    socket.getaddrinfo = local_only
    logging.disable(logging.CRITICAL)


def git_fixture(home):
    repo = home / 'repo'
    if not (repo / '.git').exists():
        repo.mkdir(exist_ok=True)
        def git(*argv):
            subprocess.run(['git', *argv], cwd=repo, check=True, capture_output=True, timeout=20)
        git('init', '-q', '-b', 'main')
        git('config', 'user.name', 'Read probe')
        git('config', 'user.email', 'probe@example.invalid')
        (repo / 'tracked').write_text('one\n', encoding='utf-8')
        git('add', 'tracked')
        git('commit', '-qm', 'fixture')
        (repo / 'untracked').write_text('two\n', encoding='utf-8')
    return repo


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def quantiles(values):
    ordered = sorted(values)
    return {'n': len(values), 'p50': round(ordered[(len(values) - 1) // 2], 3),
            'p90': round(ordered[(9 * len(values) + 9) // 10 - 1], 3)}


def exercise(args, home):
    import asyncio
    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway import profile_roster_cache
    from tui_gateway.transport import bind_transport
    from hermes_cli.web_routers.git import git_status_route
    import hermes_family_systemd
    # Snapshot liveness is deliberately unknown; never query or control host units.
    hermes_family_systemd.systemd_signals = lambda _: {}
    class Peer:
        def write(self, frame):
            return True
    peer = Peer()
    bind_transport(peer)
    server._hermes_home = home
    server._profile_home = lambda name: home if name == 'default' else home / 'profiles' / name
    with SessionDB(db_path=home / 'state.db', read_only=True) as db:
        with db._read_ctx() as conn:
            root = conn.execute("SELECT launcher_session_id FROM work_launches WHERE launcher_profile='default' "
                                'GROUP BY launcher_session_id ORDER BY count(*) DESC LIMIT 1').fetchone()[0]
        server._sessions['probe'] = {'session_key': root, 'profile_home': str(home), 'transport': peer,
            'agent_running': False, 'agent': SimpleNamespace(session_id=root, _session_db=db), 'history': []}
        calls = {
            'session.family': {'profile': 'default', 'session_id': root},
            'family.overview': {}, 'profiles.list': {}, 'model.options': {},
            'skills.changes.list': {'profile': 'default', 'session_id': root},
            'subagent.list': {'session_id': 'probe'},
        }
        repo = git_fixture(args.home)
        loop = asyncio.new_event_loop()
        results = {}
        try:
            for method, params in [*calls.items(), ('GET /api/git/status', {})]:
                def call():
                    if method.startswith('GET'):
                        return loop.run_until_complete(git_status_route(str(repo)))
                    return server.handle_request({'id': 1, 'method': method, 'params': params})
                call()  # Imports/catalog fallback are excluded from measured application-cold calls.
                cold, warm, hashes = [], [], []
                for _ in range(args.iterations):
                    profile_roster_cache.invalidate()
                    server._MODEL_OPTIONS_CACHE.clear()
                    for times in (cold, warm):
                        start = time.perf_counter()
                        value = call()
                        times.append((time.perf_counter() - start) * 1000)
                        if isinstance(value, dict) and 'error' in value:
                            raise RuntimeError(f'{method}: error code {value["error"]["code"]}')
                        hashes.append(digest(value))
                profile = cProfile.Profile()
                if args.profile:
                    profile_roster_cache.invalidate()
                    server._MODEL_OPTIONS_CACHE.clear()
                    profile.runcall(call)
                hot = []
                if args.profile:
                    stats = pstats.Stats(profile)
                    hot = sorted([(f'{Path(k[0]).name}:{k[1]}:{k[2]}', v[0], round(v[2]*1000, 2), round(v[3]*1000, 2))
                                  for k, v in stats.stats.items()], key=lambda row: row[3], reverse=True)[:15]
                results[method] = {'cold': quantiles(cold), 'warm': quantiles(warm), 'hashes': sorted(set(hashes)), 'hot': hot}
                print(json.dumps({'method': method, 'cold': quantiles(cold), 'warm': quantiles(warm)}), flush=True)
        finally:
            loop.close()
    return results


def sweep(paths):
    from hermes_family import Family, read_profile
    results = {}
    for path in paths:
        profile = path.parent.name
        snapshot = read_profile(profile, path)
        overview = Family({profile: snapshot}).overview(since=0)
        # Every stored session/launch/alias/owner, plus the complete projection for each profile.
        results[profile] = {'sessions': len(snapshot.sessions), 'launches': len(snapshot.launches),
                            'snapshot': digest(asdict(snapshot)), 'overview': digest(overview)}
        print(json.dumps({'swept_profile': profile, 'sessions': len(snapshot.sessions),
                          'launches': len(snapshot.launches)}), flush=True)
    return results


def main():
    args = arguments()
    args.source = args.source.resolve()
    sys.path.insert(0, str(args.source))
    home, paths = sandbox(args)
    offline()
    if args.prepare_lookups:
        if args.lookups is None:
            raise ValueError('--prepare-lookups requires --lookups')
        prepare_lookups(args, paths)
        return
    readonly_connections(args, paths)
    time.time = lambda: args.now
    result = {'iterations': args.iterations, 'now': args.now, 'handlers': exercise(args, home)}
    if args.sweep:
        result['sweep'] = sweep(paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    args.output.chmod(0o600)


if __name__ == '__main__':
    main()
