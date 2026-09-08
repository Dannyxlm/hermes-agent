"""Exclusion policy covers git worktrees, recovered roots and symlink paths."""

import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / 'user'
    home.mkdir()
    state = home / 'state'
    state.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(state))
    monkeypatch.setenv('HERMES_TEST_ISOLATION', str(state))
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS', '1')
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    import tui_gateway.server as server
    from hermes_state import SessionDB
    token = set_hermes_home_override(state)
    monkeypatch.setattr(server, '_hermes_home', state)
    db = SessionDB(db_path=state / 'state.db')
    monkeypatch.setattr(server, '_db', db)
    server.git_probe.invalidate()
    try:
        yield server, state, db
    finally:
        server.git_probe.invalidate()
        db.close()
        reset_hermes_home_override(token)


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), '-c', 'user.name=Review', '-c', 'user.email=review@example.invalid', '-c', 'commit.gpgsign=false', *args], check=True, capture_output=True, text=True).stdout


def init_repo(path):
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    git(path, 'commit', '--allow-empty', '-m', 'fixture', '-q')
    return path


def configure(state, excludes, roots=None):
    (state / 'config.yaml').write_text(json.dumps({'desktop': {'repo_scan_enabled': False, 'repo_scan_roots': roots or [], 'repo_scan_exclude_paths': [str(p) for p in excludes]}}))


def call(server, method, params=None):
    result = server._methods[method](1, params or {})
    assert 'error' not in result, result
    return result['result']


def add_session(db, cwd):
    db.create_session('example', 'cli', cwd=str(cwd))
    db.append_message('example', 'user', 'test-only message')


def ids(tree):
    return {p['id'] for p in tree['projects']}


def test_regular_git_repo_excluded_but_explicit_preserved(env, tmp_path):
    server, state, db = env
    excluded = tmp_path / 'releases'
    repo = init_repo(excluded / 'version-1')
    configure(state, [excluded])
    add_session(db, repo)
    assert ids(call(server, 'projects.tree')) == {'__no_project__'}
    created = call(server, 'projects.create', {'name': 'Explicit', 'folders': [str(repo)]})['project']
    assert ids(call(server, 'projects.tree')) == {created['id']}


def test_excluded_worktree_goes_home(env, tmp_path):
    server, state, db = env
    repo = init_repo(tmp_path / 'src')
    excluded = tmp_path / 'releases'
    excluded.mkdir()
    worktree = excluded / 'version-1'
    git(repo, 'worktree', 'add', '--detach', str(worktree), 'HEAD')
    configure(state, [excluded])
    add_session(db, worktree)
    discovery = call(server, 'projects.discover_repos')
    tree = call(server, 'projects.tree')
    assert ids(tree) == {'__no_project__'}, {'tree': tree, 'discovery': discovery}


def test_recovered_excluded_repo_stays_out_of_tree(env, tmp_path):
    server, state, db = env
    repo = init_repo(tmp_path / 'repo')
    configure(state, [repo])
    add_session(db, tmp_path / 'repo-topic')
    tree = call(server, 'projects.tree')
    assert ids(tree) == {'__no_project__'}, tree


def test_remote_scan_keeps_excluded_symlink_root_out(env, tmp_path):
    server, state, db = env
    repo = init_repo(tmp_path / 'actual')
    excluded = tmp_path / 'releases'
    excluded.mkdir()
    link = excluded / 'current'
    link.symlink_to(repo, target_is_directory=True)
    from hermes_cli import projects_db as pdb
    policy = {'enabled': True, 'roots': [str(link)], 'exclude_paths': [str(excluded)]}
    with pdb.connect_closing() as conn:
        server._scan_discovered_repos_remote(conn, policy)
        rows = pdb.list_discovered_repos(conn)
    assert not rows, rows
