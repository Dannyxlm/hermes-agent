"""Shared-resource leases use real SQLite and never confer authority across epochs."""
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_shared_resource_foreign_holder_and_expiry(tmp_path):
    from tools.desktop_space_lease import LeaseStore, LeaseConflict
    path = tmp_path / 'spaces.sqlite'
    a, b = LeaseStore(path), LeaseStore(path)
    first = a.acquire('studio', 'profile-a', 'viewer-a', expected_epoch=0, now=10, ttl=30)
    assert first.holder == 'human' and first.epoch == 1
    with pytest.raises(LeaseConflict):
        b.acquire('studio', 'profile-b', 'viewer-b', expected_epoch=1, now=11, ttl=30)
    with pytest.raises(LeaseConflict):
        b.release('studio', 'profile-b', 'viewer-a', expected_epoch=1, now=11)
    assert not a.permits('studio', 'profile-a', 'viewer-a', 1, now=40)
    assert not a.agent_permitted('studio', 1)
    second = b.acquire('studio', 'profile-b', 'viewer-b', expected_epoch=1, now=41, ttl=30)
    assert second.epoch == 2
    with pytest.raises(LeaseConflict):
        a.release('studio', 'profile-a', 'viewer-a', expected_epoch=1, now=42)
    released = b.release('studio', 'profile-b', 'viewer-b', expected_epoch=2, now=42)
    assert released.holder == 'agent' and released.epoch == 3
    assert b.agent_permitted('studio', 3)
    assert not b.permits('studio', 'profile-b', 'viewer-b', 2, now=43)


def test_concurrent_takeover_has_one_winner(tmp_path):
    from tools.desktop_space_lease import LeaseStore, LeaseConflict
    path = tmp_path / 'spaces.sqlite'
    LeaseStore(path).initialize()
    def acquire(i):
        try:
            return LeaseStore(path).acquire('desktop', str(i), str(i), expected_epoch=0, now=10, ttl=60).epoch
        except LeaseConflict:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(acquire, range(4)))
    assert result.count(1) == 1
    assert result.count(None) == 3


def test_resources_are_distinct_and_corrupt_db_fails_closed(tmp_path):
    from tools.desktop_space_lease import LeaseStore
    path = tmp_path / 'spaces.sqlite'
    store = LeaseStore(path)
    assert store.read('studio').holder == 'none'
    assert not path.exists()
    store.acquire('desktop', 'owner', 'viewer', expected_epoch=0, now=10, ttl=60)
    assert store.read('studio').epoch == 0
    path.write_bytes(b'broken sqlite')
    with pytest.raises(Exception):
        store.read('desktop')
    with pytest.raises(ValueError):
        store.acquire('../arbitrary', 'owner', 'viewer', expected_epoch=0, now=10, ttl=60)


@pytest.mark.parametrize('ttl', [0, -1, 301, float('nan')])
def test_unbounded_leases_refused(tmp_path, ttl):
    from tools.desktop_space_lease import LeaseStore
    with pytest.raises(ValueError):
        LeaseStore(tmp_path / 'spaces.sqlite').acquire('studio', 'owner', 'viewer', expected_epoch=0, now=10, ttl=ttl)
