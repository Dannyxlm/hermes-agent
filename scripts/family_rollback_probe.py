"""Exercise a clean predecessor export against an additively migrated disposable DB."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-export', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / 'rollback.db'
    if path.exists():
        parser.error('probe requires a fresh disposable database')
    head = Path(__file__).resolve().parents[1]
    env = dict(os.environ, HERMES_HOME=str(scratch / 'home'), HOME=str(scratch / 'user'),
               TMPDIR=str(scratch))
    env.pop('PYTHONPATH', None)
    snippets = [
        (args.base_export, "db.create_session('predecessor','cli'); assert len(db.list_sessions_rich()) == 1"),
        (head, "db.record_launch('probe',kind='lane',launcher_profile='default',launcher_session_id='predecessor',"
               "child_profile='default'); db.create_session('child','cli'); "
               "db.bind_launch('probe',child_session_id='child'); assert db.get_session('child')['last_activity_kind'] is None"),
        (args.base_export, "assert len(db.list_sessions_rich()) == 2; db.create_session('old-writer','cli'); "
                           "db.end_session('old-writer','agent_close'); assert db.get_session('old-writer')['ended_at']; "
                           "assert db.delete_session('child'); assert db.get_session('child') is None"),
        (head, "assert db.get_session('child') is None; assert db.get_session('old-writer')['ended_at']; "
               "db.finish_launch('probe','success'); "
               "assert db._read_one('SELECT terminal_cause FROM work_launches WHERE launch_id=?',('probe',))[0] == 'success'"),
    ]
    for index, (tree, snippet) in enumerate(snippets, 1):
        code = ("from pathlib import Path\nfrom hermes_state import SessionDB\n"
                f"with SessionDB(Path({str(path)!r})) as db:\n    {snippet}\n")
        subprocess.run([sys.executable, '-c', code], cwd=tree, env=env, check=True, timeout=60)
        print(json.dumps({'stage': index, 'binary': 'head' if tree == head else 'predecessor', 'ok': True}), flush=True)


if __name__ == '__main__':
    main()
