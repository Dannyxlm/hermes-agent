"""Canonical profile roster reads use on-demand cached skill counts, never a recursive scan."""
from types import SimpleNamespace

from gateway.session_contract import Principal
from gateway.session_group_controls import _profiles


def test_canonical_roster_uses_the_polled_profile_metadata_path(tmp_path, monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(profiles, '_get_profiles_root', lambda: tmp_path / 'profiles')
    monkeypatch.setattr(profiles, '_cached_skill_count', lambda home: 7)

    def forbidden(home):
        raise AssertionError('roster synchronously scanned the skill tree')

    monkeypatch.setattr(profiles, '_count_skills', forbidden)
    authority = SimpleNamespace(profile_id=str(tmp_path), hosted_room_service=None)
    actor = Principal('owner', str(tmp_path), frozenset({'session:read'}), 'fixture')
    result = _profiles(authority, actor, tmp_path, {'include_sessions': False})
    assert result['profiles'][0]['skill_count'] == 7
