"""The model-facing bridge must advertise its actual batching boundary."""

import re

import pytest

from tools.tool_search import bridge_tool_schemas


@pytest.mark.parametrize("surface", ["tool", "calls"])
def test_tool_call_describes_local_single_entry_and_connector_only_batching(surface):
    schema = next(td["function"] for td in bridge_tool_schemas(1)
                  if td["function"]["name"] == "tool_call")
    description = (schema["description"] if surface == "tool"
                   else schema["parameters"]["properties"]["calls"]["description"])

    # Both surfaces can guide argument construction; neither may invite local batches.
    assert re.search(r"exactly one entry[^.]*local", description, re.IGNORECASE)
    assert re.search(r"only[^.]*connectors__[^.]*batch", description, re.IGNORECASE)
