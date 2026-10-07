"""Rename containment and published legacy projection recovery."""
import sqlite3
import pytest
from plugins.cloudseed_mobile import deliverables as d, deliverables_index as ix
from tests.plugins.test_cloudseed_mobile import client, P
from tests.plugins.cloudseed_mobile.test_workspace_files import setup_root
from tests.plugins.cloudseed_mobile.test_deliverables_index import store


@pytest.fixture(autouse=True)
def _index_semantics_over_synthetic_paths(monkeypatch):
    """These tests pin index, keyset and admission semantics over synthetic paths that
    are never written to disk. Recent's existence check has its own tests in
    test_deliverables_recent_exists.py."""
    monkeypatch.setattr(ix.DeliverablesIndex, '_exists', lambda self, row: True)


def test_renamed_workspace_deliveries_open_without_new_grants(client):
    c, home = client
    root, wid = setup_root(c, home)
    alias = root.parent / 'CloudSeed Strategy'
    alias.symlink_to(root.name)
    (root / 'outputs/report.md').write_text('synthetic delivery')
    outside = home / 'outside'; outside.mkdir()
    (outside / 'report.md').write_text('not granted')
    external = root.parent / 'late-night-lips-lookbook'; external.symlink_to(outside)
    chained = root.parent / 'Chained'; chained.symlink_to(alias.name)
    (root / 'escape').symlink_to(outside)
    grants = c.get(P + '/workspaces?profile=default').json()['items']
    assert {g['id'] for g in grants} == {wid}
    for session, value in [({'id':'s', 'cwd':str(alias)}, 'outputs/report.md'),
                           ({'id':'s', 'cwd':str(root)}, str(alias / 'outputs/report.md'))]:
        rows = d.project('default', session, [{'id':1,'role':'assistant','content':'MEDIA:' + value}], grants)
        assert [(r['workspace_id'], r['relative_path']) for r in rows] == [(wid, 'outputs/report.md')]
        response = c.get(P + '/workspace-files/download', params={'profile':'default','workspace_id':wid,'path':rows[0]['relative_path']})
        assert response.content == b'synthetic delivery'
    for path in [external / 'report.md', chained / 'outputs/report.md', alias / '../outside/report.md']:
        assert d.locator(str(path), {'cwd':str(root)}, grants) is None
    row = d.locator(str(alias / 'escape/report.md'), {}, grants)
    assert c.get(P + '/workspace-files/download', params={'profile':'default','workspace_id':wid,'path':row['relative_path']}).status_code == 403


def test_scan_v3_false_deliveries_rebuilt_out_of_recent(client, monkeypatch):
    c, home = client
    root, wid = setup_root(c, home); store(home, root, 1)
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('UPDATE messages SET content=?', ('The syntax is MEDIA:outputs/example.pdf\n```text\nMEDIA:outputs/fenced.pdf\n```\nMEDIA:outputs/real.pdf',))
    grants = [{'id':wid,'root_path':str(root),'name':'Fixture'}]
    index = ix.DeliverablesIndex(home, 'default', grants)
    # Freeze a published v3 cache with its old admission policy.
    def legacy(text, **kwargs):
        for line in text.splitlines():
            if 'MEDIA:' in line: yield line.split('MEDIA:', 1)[1], 'delivered'
    with monkeypatch.context() as m:
        m.setattr(d, 'references', legacy)
        index.refresh()
    with sqlite3.connect(index.path) as db:
        db.execute("UPDATE meta SET value='3' WHERE key='scan_version'")
    assert len(index.query()['items']) == 3
    index.refresh()
    assert {r['relative_path'] for r in index.query()['items']} == {'outputs/real.pdf'}
    assert {r['relative_path'] for r in index.query(session_id='0')['items']} == {'outputs/real.pdf','outputs/example.pdf','outputs/fenced.pdf'}
