"""Contained native files routes, using synthetic roots only."""
import os
import pytest
from fastapi import HTTPException
from tests.plugins.test_cloudseed_mobile import client, P


def setup_root(c, home):
    root = home.parent/('workspaces-'+home.name)/'Fixture'
    (root/'outputs').mkdir(parents=True)
    (home/'config.yaml').write_text('cloudseed_mobile:\n  workspace_root: '+str(root.parent)+'\n')
    response = c.get(P+'/workspaces?profile=default')
    assert response.status_code == 200
    return root, response.json()['items'][0]['id']


def test_files_preview_original_and_stream(client):
    c, home = client
    root, wid = setup_root(c, home)
    data = b'a'*(512*1024)+b'\xfftail'
    (root/'outputs/report.md').write_bytes(data)
    params = {'profile':'default','workspace_id':wid,'path':'outputs/report.md'}
    preview = c.get(P+'/workspace-files/read', params=params).json()
    assert preview['truncated'] and preview['byteSize'] == len(data)
    assert preview['mime'] == 'text/markdown'
    assert c.get(P+'/workspace-files/download', params=params).content == data
    (root/'clip.mp4').write_bytes(b'0123456789')
    params['path'] = 'clip.mp4'
    url = P+'/workspace-files/stream'
    r = c.get(url, params=params, headers={'Range':'bytes=2-5'})
    assert r.status_code == 206 and r.content == b'2345'
    assert r.headers['content-range'] == 'bytes 2-5/10'
    assert c.get(url, params=params, headers={'Range':'bytes=20-'}).status_code == 416
    assert c.head(url, params=params).content == b''
    assert c.head(url, params=params).headers['content-length'] == '10'


@pytest.mark.parametrize('path', ['../outside','/etc/passwd','outputs/../../outside','%2e%2e/outside','%252e%252e/outside','a\x00b','.SSH/key','Credentials/key','.ENV','state.db','auth.json','outputs/private/x'])
def test_sensitive_and_traversal_denied(client, path):
    c, home = client
    root, wid = setup_root(c, home)
    for route in ['read','download','stream']:
        assert c.get(P+'/workspace-files/'+route, params={'profile':'default','workspace_id':wid,'path':path}).status_code in (400,403,404)


def test_metadata_private_symlink_nonregular_and_auth(client):
    c, home = client
    root, wid = setup_root(c, home)
    (root/'outputs/private').mkdir()
    (root/'outputs/private/x').write_text('private')
    (root/'.Env').write_text('secret')
    (root/'escape').symlink_to(home)
    os.mkfifo(root/'pipe')
    params = {'profile':'default','workspace_id':wid,'q':'','path':''}
    listing = c.get(P+'/workspace-files',params=params).json()
    assert not {'.Env','escape','pipe'} & {i['name'] for i in listing['items']}
    params.update(path='outputs/private/x')
    assert c.get(P+'/workspace-files/read',params=params).status_code == 403
    params['reveal'] = 1
    assert c.get(P+'/workspace-files/read',params=params).json()['text'] == 'private'
    params['path'] = '.Env'
    assert c.get(P+'/workspace-files/read',params=params).status_code == 403
    params['workspace_id'] = 'unknown'
    assert c.get(P+'/workspace-files',params=params).status_code == 404
    c.headers.pop('Authorization')
    assert c.get(P+'/workspaces?profile=default').status_code == 401


def test_workspace_listing_search_caps_cursor_and_symlinks(client):
    c,home=client; root,wid=setup_root(c,home)
    (root/'outputs/a').write_text('a'); (root/'outputs/b').write_text('b')
    (root/'escape').symlink_to(home/'state.db')
    os.mkfifo(root/'pipe')
    params={'profile':'default','workspace_id':wid,'path':'outputs','limit':1}
    page=c.get(P+'/workspace-files',params=params).json()
    params['cursor']=page['next_cursor']
    assert c.get(P+'/workspace-files',params=params).json()['items'][0]['name']=='b'
    params['q']='different'
    assert c.get(P+'/workspace-files',params=params).status_code==409
    for path in ['escape','pipe']:
        assert c.get(P+'/workspace-files/download',params={'profile':'default','workspace_id':wid,'path':path}).status_code==403
    assert c.get(P+'/workspace-files',params={'profile':'default','workspace_id':wid,'limit':201}).status_code==400
    from plugins.cloudseed_mobile import workspace_files as files
    old=files.MAX_ENTRIES; files.MAX_ENTRIES=1
    try:
        assert c.get(P+'/workspace-files',params={'profile':'default','workspace_id':wid,'q':'a'}).json()['partial']
    finally: files.MAX_ENTRIES=old
    c.headers.pop('Authorization'); c.cookies.set('fixture_session','owner')
    (root/'clip.mp4').write_bytes(b'bytes')
    assert c.head(P+'/workspace-files/stream',params={'profile':'default','workspace_id':wid,'path':'clip.mp4'}).status_code==200


def test_default_cwd_does_not_grant_unrelated_siblings(client):
    c,home=client
    parent=home.parent/('projects-'+home.name); parent.mkdir()
    repo=parent/'Repo'; repo.mkdir(); unrelated=parent/'Other'; unrelated.mkdir()
    (home/'config.yaml').write_text('terminal:\n  cwd: '+str(repo)+'\n')
    response=c.get(P+'/workspaces?profile=default')
    assert {row['root_path'] for row in response.json()['items']}=={str(repo)}


def test_sensitive_native_credential_floor(client):
    c,home=client; root,wid=setup_root(c,home)
    from plugins.cloudseed_mobile.workspace_files import parts
    for path in ['MCP-TOKENS/a.json','Pairing/a.json','Google_Token.json','BWS_CACHE.ENC.JSON','.anthropic_oauth.json','webhook_subscriptions.json']:
        with pytest.raises(HTTPException): parts(path,True)


def test_descriptor_replacement_race(client, monkeypatch):
    from plugins.cloudseed_mobile import workspace_files as files
    c, home = client
    root, wid = setup_root(c, home)
    (root/'dir').mkdir()
    (root/'dir/x').write_text('inside')
    outside = home/'outside'; outside.mkdir(); (outside/'x').write_text('secret')
    original = os.open
    def swapped(path, flags, *args, **kwargs):
        if path == 'x':
            (root/'dir').rename(root/'old')
            (root/'dir').symlink_to(outside)
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr(files.os, 'open', swapped)
    r = c.get(P+'/workspace-files/read',params={'profile':'default','workspace_id':wid,'path':'dir/x'})
    assert r.status_code in (200,403)
    if r.status_code == 200:
        assert r.json()['text'] == 'inside'


def test_native_media_originals_profile_scope_and_symlinks(client):
    c, home = client
    other = home / 'profiles' / 'b'
    other.mkdir(parents=True)
    (other / 'config.yaml').write_text('{}')
    for profile, base in [('default', home), ('b', other)]:
        for folder, name in [('images', 'upload.png'), ('screenshots', 'capture.png'), ('cache/audio', 'voice.mp3'), ('attachments', 'original.pdf')]:
            directory = base / folder
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes((profile + ':' + name).encode())
    for profile, base in [('default', home), ('b', other), ('default', home)]:
        for folder, name in [('images', 'upload.png'), ('screenshots', 'capture.png'), ('cache/audio', 'voice.mp3'), ('attachments', 'original.pdf')]:
            params = {'profile': profile, 'media_path': str(base / folder / name)}
            response = c.get(P + '/workspace-files/download', params=params)
            assert response.status_code == 200
            assert response.content == (profile + ':' + name).encode()
        response = c.get(P + '/workspace-files/stream', params={'profile': profile, 'media_path': str(base / 'cache/audio/voice.mp3')}, headers={'Range': 'bytes=1-3'})
        assert response.status_code == 206 and response.content == (profile + ':voice.mp3').encode()[1:4]
    assert c.get(P + '/workspace-files/download', params={'profile': 'default', 'media_path': str(other / 'images/upload.png')}).status_code == 403
    (home / 'images/escape.png').symlink_to(other / 'images/upload.png')
    (home / 'cache/auth.json').write_text('synthetic private file')
    for path in [home / 'images/escape.png', home / 'config.yaml', home / 'cache/auth.json', home / 'images/../config.yaml']:
        assert c.get(P + '/workspace-files/download', params={'profile': 'default', 'media_path': str(path)}).status_code == 403
    c.headers.pop('Authorization')
    assert c.get(P + '/workspace-files/download', params={'profile': 'default', 'media_path': str(home / 'images/upload.png')}).status_code == 401


def test_producer_resolved_legacy_media_and_browser_screenshots(client):
    from hermes_constants import get_hermes_dir, set_hermes_home_override, reset_hermes_home_override
    c, home = client
    other = home / 'profiles' / 'legacy'
    other.mkdir(parents=True)
    (other / 'config.yaml').write_text('{}')
    folders = [('audio_cache', 'voice.mp3'), ('image_cache', 'image.png'),
               ('video_cache', 'clip.mp4'), ('document_cache', 'doc.pdf'),
               ('browser_screenshots', 'capture.png')]
    for profile, base in [('default', home), ('legacy', other)]:
        for folder, filename in folders:
            (base / folder).mkdir()
            (base / folder / filename).write_bytes((profile + ':' + filename).encode())
    for profile, base in [('default', home), ('legacy', other), ('default', home)]:
        token = set_hermes_home_override(base)
        try:
            audio = get_hermes_dir('cache/audio', 'audio_cache') / 'voice.mp3'
            assert audio == base / 'audio_cache/voice.mp3'
        finally:
            reset_hermes_home_override(token)
        for folder, filename in folders:
            response = c.get(P + '/workspace-files/download', params={'profile': profile, 'media_path': str(base / folder / filename)})
            assert response.status_code == 200
            assert response.content == (profile + ':' + filename).encode()
    assert c.get(P + '/workspace-files/download', params={'profile': 'default', 'media_path': str(other / 'audio_cache/voice.mp3')}).status_code == 403
    (home / 'audio_cache/escape.mp3').symlink_to(other / 'audio_cache/voice.mp3')
    assert c.get(P + '/workspace-files/download', params={'profile': 'default', 'media_path': str(home / 'audio_cache/escape.mp3')}).status_code == 403
