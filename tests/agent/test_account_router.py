"""Smart routing behavior through pure scoring and the real credential chokepoint."""
import time

from agent.account_router.types import AccountMeter, MeterWindow
from agent.account_router.scorer import rank


def meter(id, used=0, units="default_claude_ai", now=100000, scope=None, hours=48):
    return AccountMeter("anthropic", id, units,
                        (MeterWindow("weekly", used, now + hours * 3600, scope),), now, "usage_api")


def test_fable_scoped_capacity_and_plan_size():
    now = 100000
    meters = {"big": meter("big", 100, "default_claude_max_20x", scope="fable"),
              "max": meter("max", 0, "default_claude_max_5x", scope="fable"),
              "pro": meter("pro", 8, scope="fable")}
    rows = rank("anthropic", "claude-fable-5-2", ["big", "pro", "max"], meters, now, {})
    assert [r[0] for r in rows] == ["max", "pro", "big"]
    assert rows[-1][1] == 0


def test_opus_binding_window_and_unknown():
    now = 100000
    meters = {"max20": meter("max20", 68, "default_claude_max_20x", hours=47),
              "pro": meter("pro", 0)}
    assert rank("anthropic", "claude-opus-4-6", ["pro", "max20"], meters, now, {})[0][0] == "max20"
    assert [r[0] for r in rank("anthropic", None, ["a", "b"], {}, now, {})] == ["a", "b"]
    assert [r[0] for r in rank("anthropic", None, ["pro", "max20"], meters, now+2000, {})] == ["pro", "max20"]


def test_known_unknown_and_cap_order():
    now = 100000
    meters = {"cap": meter("cap", 99), "good": meter("good")}
    assert [r[0] for r in rank("anthropic", None, ["cap", "unknown", "good"], meters, now, {})] == ["good", "unknown", "cap"]


def test_real_pool_shared_snapshot_and_model_pin(tmp_path, monkeypatch):
    from agent.account_router.snapshot import merge_meters, pin, read_snapshot, snapshot_path
    from agent.credential_pool import CredentialPool, PooledCredential
    now = time.time()
    entries = [PooledCredential(id=id, provider="anthropic", label=id, priority=i,
                               auth_type="api_key", source="manual", access_token="fixture-"+id)
               for i, id in enumerate(["a", "b"])]
    path = snapshot_path("anthropic")
    merge_meters(path, [meter("a", 90, now=now), meter("b", 0, now=now)], policy={}, now=now)
    monkeypatch.setattr("agent.credential_pool.get_pool_strategy", lambda p: "smart")
    pools = [CredentialPool("anthropic", entries) for _ in range(2)]
    assert [p._available_entries()[0][0].id for p in pools] == ["b", "b"]
    before = path.stat().st_mtime_ns
    assert {e.id for e in pools[0]._available_entries()[0]} == {"a", "b"}
    assert path.stat().st_mtime_ns == before
    pin("anthropic", "a", path=path, now=now)
    assert pools[0]._available_entries()[0][0].id == "a"
    state = read_snapshot(path)
    assert state["pins"]["anthropic"]["entry_id"] == "a"


def test_router_fail_open(monkeypatch):
    from agent.account_router import router
    from agent.credential_pool import CredentialPool, PooledCredential
    entries = [PooledCredential(id=id, provider="anthropic", label=id, priority=i,
                               auth_type="api_key", source="manual", access_token="fixture-"+id)
               for i, id in enumerate(["a", "b"])]
    monkeypatch.setattr(router, "reorder", lambda *a, **k: (_ for _ in ()).throw(ValueError("fixture")))
    monkeypatch.setattr("agent.credential_pool.get_pool_strategy", lambda p: "smart")
    assert [e.id for e in CredentialPool("anthropic", entries)._available_entries()[0]] == ["a", "b"]
