"""Multiple launch receipts for one session have stable unfinished-first precedence."""
from itertools import permutations

import pytest

from hermes_family import Family, Profile, node_id


@pytest.mark.parametrize("order", list(permutations(["old_live", "new_live", "finished"])))
def test_family_prefers_latest_unfinished_launch_independent_of_query_order(order):
    launches = {key: dict(launch_id=key, kind="lane", launcher_profile="default", launcher_session_id="parent",
                         child_profile="default", child_session_id="child", created_at=created,
                         finished_at=ended, terminal_cause=cause)
                for key, created, ended, cause in [("old_live", 1, None, None), ("new_live", 2, None, None),
                                                   ("finished", 3, 4, "success")]}
    profile = Profile("default", {"parent": {"id": "parent"}, "child": {"id": "child", "turn_leased": True}},
                      [launches[key] for key in order])
    graph = Family({"default": profile})
    node, = graph.session_family("default", "parent", since=0)["nodes"]
    assert node["status"] == "working" and node["ended_at"] is None
    assert graph.rows[node_id("default", "child")]["launch_id"] == "new_live"
    for launch in profile.launches:
        launch.update(finished_at=5, terminal_cause="success")
    graph = Family({"default": profile})
    assert graph.rows[node_id("default", "child")]["launch_id"] == "finished"
