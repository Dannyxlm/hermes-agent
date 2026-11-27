"""A Paper card's files load on the phone: what ``show_card`` accepts, the app's media route serves.

The app fetches every card file (gallery tiles, a compose hero or image block, a link image, a document) with
``GET /workspace-files/download?profile=…&media_path=<the card's absolute path>``. That route serves Hermes's media
roots, and now exactly the files an accepted card granted; everything else stays refused. Real ``show_card``
through the registry and dispatcher, the real plugin router, synthetic files outside the profile home.
"""
import json
import os
import uuid

import pytest

import model_tools
from tests.plugins.test_cloudseed_mobile import client as _client, P
from tools.registry import invalidate_check_fn_cache

client = _client
DOWNLOAD = P + '/workspace-files/download'


@pytest.fixture
def cards(client):
    c, home = client
    (home / 'config.yaml').write_text('cards:\n  enabled: true\n', encoding='utf-8')
    invalidate_check_fn_cache()
    assets = home.parent / ('lnl-' + home.name) / 'outputs/brand-core/assets'  # a workspace, outside every media root
    assets.mkdir(parents=True)
    yield c, home, assets
    invalidate_check_fn_cache()


def show(card, call_id='call-1'):
    return json.loads(model_tools.handle_function_call(
        'show_card', card, 'task-1', tool_call_id=call_id, session_id='s-1', turn_id=uuid.uuid4().hex))


def fetch(c, path, profile='default'):
    return c.get(DOWNLOAD, params={'profile': profile, 'media_path': str(path)})


def jpg(directory, name):
    path = directory / name
    path.write_bytes(b'\xff\xd8\xff' + name.encode())
    return path


def test_the_reported_gallery_loads_every_tile_through_the_route_the_phone_uses(cards):
    c, _home, assets = cards
    names = ['g-apply-teal.jpg', 'g-beret-apply.jpg', 'g-corridor.jpg', 'g-fur-blue.jpg',
             'g-fur-kiss.jpg', 'g-fur-lips.jpg', 'g-redsofa.jpg', 'g-tube-screens.jpg']
    files = [jpg(assets, name) for name in names]
    before = fetch(c, files[0])
    assert before.status_code == 403  # the bug, pinned: validation alone never made a file fetchable

    receipt = show({'kind': 'gallery', 'title': 'Late Night Lips brand book, vol. 01',
                    'data': {'images': [{'file': str(path)} for path in files]}})
    assert receipt['ok'] is True, receipt
    for path in files:
        response = fetch(c, path)
        assert response.status_code == 200, (path.name, response.text)
        assert response.content == path.read_bytes()
        assert response.headers['content-type'] == 'image/jpeg'
        assert response.headers['cache-control'] == 'no-store'


def test_compose_hero_compose_image_link_image_and_document_share_the_grant(cards):
    c, _home, assets = cards
    hero, block, link_image = jpg(assets, 'hero.jpg'), jpg(assets, 'block.jpg'), jpg(assets, 'og.jpg')
    lease = assets / 'lease.pdf'
    lease.write_bytes(b'%PDF-1.7 synthetic')
    assert show({'kind': 'compose', 'title': 'The week', 'alt': 'The week in one picture.',
                 'data': {'hero': {'type': 'image', 'file': str(hero)},
                          'blocks': [{'type': 'text', 'text': 'Shipped.'},
                                     {'type': 'image', 'file': str(block)}]}}, 'c1')['ok']
    assert show({'kind': 'link', 'title': 'Lookbook', 'data': {'url': 'https://example.com/lookbook',
                                                               'image': str(link_image)}}, 'c2')['ok']
    assert show({'kind': 'document', 'title': 'Lease', 'data': {'file': str(lease)}}, 'c3')['ok']
    for path in (hero, block, link_image, lease):
        response = fetch(c, path)
        assert response.status_code == 200, path.name
        assert response.content == path.read_bytes()


def test_only_the_cards_own_files_are_served(cards):
    c, home, assets = cards
    shown, neighbour = jpg(assets, 'shown.jpg'), jpg(assets, 'neighbour.jpg')
    assert show({'kind': 'gallery', 'title': 'One', 'data': {'images': [{'file': str(shown)}]}})['ok']
    assert fetch(c, shown).status_code == 200
    assert fetch(c, neighbour).status_code == 403  # same folder, never shown
    assert fetch(c, assets).status_code == 403  # the folder itself
    assert fetch(c, home / 'config.yaml').status_code == 403
    assert fetch(c, '/etc/passwd').status_code == 403
    assert c.get(DOWNLOAD, params={'profile': 'default', 'media_path': 'outputs/shown.jpg'}).status_code == 400
    c.headers.pop('Authorization')
    assert fetch(c, shown).status_code == 401


def test_a_refused_card_grants_nothing(cards):
    c, _home, assets = cards
    shown = jpg(assets, 'shown.jpg')
    receipt = show({'kind': 'gallery', 'title': 'x' * 81, 'data': {'images': [{'file': str(shown)}]}})
    assert receipt['ok'] is False
    assert fetch(c, shown).status_code == 403


def test_secret_bearing_files_are_refused_by_the_validator_and_never_served(cards):
    c, _home, assets = cards
    for relative in ('.ssh/id_ed25519', '.aws/credentials', 'tls/server.pem', 'keys/signing.key'):
        path = assets / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic key material')
        receipt = show({'kind': 'document', 'title': 'Key', 'data': {'file': str(path)}}, relative)
        assert receipt['ok'] is False, relative
        assert receipt['errors'][0]['path'] == 'data.file'
        assert 'key or credential' in receipt['errors'][0]['message']
        assert fetch(c, path).status_code == 403, relative


def test_a_grant_never_follows_a_symlink_swapped_in_after_the_card(cards):
    c, home, assets = cards
    shown = jpg(assets, 'shown.jpg')
    assert show({'kind': 'gallery', 'title': 'One', 'data': {'images': [{'file': str(shown)}]}})['ok']
    secret = home.parent / ('secret-' + home.name + '.jpg')
    secret.write_bytes(b'not for the phone')
    shown.unlink()
    shown.symlink_to(secret)  # the granted name now points elsewhere
    assert fetch(c, shown).status_code == 403
    shown.unlink()
    assets.rename(assets.with_name('moved'))
    assets.symlink_to(assets.with_name('moved'))  # a parent directory swapped for a symlink
    jpg(assets.with_name('moved'), 'shown.jpg')
    assert fetch(c, shown).status_code == 403


def test_a_granted_file_that_is_gone_is_404_and_a_rewritten_one_serves_its_new_bytes(cards):
    c, _home, assets = cards
    shown = jpg(assets, 'shown.jpg')
    assert show({'kind': 'gallery', 'title': 'One', 'data': {'images': [{'file': str(shown)}]}})['ok']
    shown.write_bytes(b'\xff\xd8\xffre-exported')
    assert fetch(c, shown).content == b'\xff\xd8\xffre-exported'
    shown.unlink()
    assert fetch(c, shown).status_code == 404


def test_a_symlinked_card_path_is_granted_as_the_file_it_resolves_to(cards):
    c, _home, assets = cards
    real = jpg(assets, 'real.jpg')
    alias = assets / 'alias.jpg'
    alias.symlink_to(real)
    assert show({'kind': 'gallery', 'title': 'One', 'data': {'images': [{'file': str(alias)}]}})['ok']
    assert fetch(c, alias).content == real.read_bytes()
    assert fetch(c, real).content == real.read_bytes()


def test_grants_are_per_profile(cards):
    c, home, assets = cards
    other = home / 'profiles' / 'b'
    other.mkdir(parents=True)
    (other / 'config.yaml').write_text('{}')
    shown = jpg(assets, 'shown.jpg')
    assert show({'kind': 'gallery', 'title': 'One', 'data': {'images': [{'file': str(shown)}]}})['ok']
    assert fetch(c, shown).status_code == 200
    assert fetch(c, shown, profile='b').status_code == 403


def test_grant_store_is_private_and_holds_only_resolved_paths(cards):
    _c, home, assets = cards
    shown = jpg(assets, 'shown.jpg')
    receipt = show({'kind': 'gallery', 'title': 'One', 'data': {'images': [{'file': str(shown)}]}})
    assert receipt['ok'], receipt
    directory = home / 'card_file_grants'
    assert oct(directory.stat().st_mode & 0o777) == '0o700'
    entries = list(directory.iterdir())
    assert len(entries) == 1 and oct(entries[0].stat().st_mode & 0o777) == '0o600'
    assert entries[0].read_text() == os.path.realpath(shown)
    assert show({'kind': 'gallery', 'title': 'Again', 'data': {'images': [{'file': str(shown)}]}}, 'c2')['ok']
    assert len(list(directory.iterdir())) == 1  # idempotent


def test_a_parent_swapped_for_a_symlink_between_check_and_open_is_refused(cards, monkeypatch):
    """The race the descriptor walk closes: the grant and predicate pass, then a directory becomes a symlink."""
    from tools.cards import validate
    c, home, assets = cards
    shown = jpg(assets, 'shown.jpg')
    assert show({'kind': 'gallery', 'title': 'One', 'data': {'images': [{'file': str(shown)}]}})['ok']
    outside = home.parent / ('outside-' + home.name)
    outside.mkdir()
    (outside / 'shown.jpg').write_bytes(b'not for the phone')
    checked = validate.server_file_problem

    def check_then_swap(path):
        problem = checked(path)
        assets.rename(assets.with_name('moved'))
        assets.symlink_to(outside)
        return problem

    monkeypatch.setattr(validate, 'server_file_problem', check_then_swap)
    response = fetch(c, shown)
    assert response.status_code == 403
    assert b'not for the phone' not in response.content


def test_backfill_grants_cards_accepted_before_grants_existed_and_nothing_else(cards):
    import sqlite3
    from tools.cards.file_grants import backfill
    c, home, assets = cards
    accepted, refused, stale = jpg(assets, 'accepted.jpg'), jpg(assets, 'refused.jpg'), jpg(assets, 'stale.jpg')
    key = assets / '.ssh' / 'photo.jpg'
    key.parent.mkdir()
    key.write_bytes(b'\xff\xd8\xff')
    db = home.parent / ('state-' + home.name + '.db')
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, '
                'tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)')

    def card_turn(call_id, files, ok):
        call = {'id': call_id, 'type': 'function', 'function': {'name': 'show_card', 'arguments': json.dumps(
            {'kind': 'gallery', 'title': 'Then', 'data': {'images': [{'file': str(f)} for f in files]}})}}
        con.execute('INSERT INTO messages (session_id, role, tool_calls) VALUES (?, ?, ?)',
                    ('s-old', 'assistant', json.dumps([call])))
        con.execute('INSERT INTO messages (session_id, role, content, tool_call_id, tool_name) VALUES (?, ?, ?, ?, ?)',
                    ('s-old', 'tool', json.dumps({'ok': ok}), call_id, 'show_card'))

    card_turn('call-ok', [accepted, stale, key], True)
    card_turn('call-refused', [refused], False)
    con.commit(); con.close()
    stale.unlink()
    for path in (accepted, refused, key):
        assert fetch(c, path).status_code == 403
    assert backfill(home=home, db_path=db) == {'cards': 1, 'granted': 1, 'skipped': 2}
    assert fetch(c, accepted).content == accepted.read_bytes()
    assert fetch(c, refused).status_code == 403  # its card was refused
    assert fetch(c, key).status_code == 403  # today's validator refuses it
    assert backfill(home=home, db_path=db)['granted'] == 1  # idempotent
