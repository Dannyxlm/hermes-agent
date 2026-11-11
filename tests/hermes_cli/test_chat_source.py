"""Chat source flags match the canonical gateway vocabulary and fail before startup."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from hermes_cli._parser import build_top_level_parser


@pytest.mark.parametrize("source", ["cli", "tui", "gui", "desktop", "acp", "tool", "oneshot"])
def test_accepted_source_reaches_session_create(monkeypatch, source):
    from hermes_cli import gateway_chat
    from hermes_cli.gateway_chat_view import GatewayChatView
    from gateway.session_policy import SURFACES
    from tui_gateway.contracts.canonical_sessions import CanonicalCreateParams

    created = []

    class Peer:
        async def rpc(self, method, **params):
            if method == "runtime.describe":
                return {"session_create": {"sources": sorted(SURFACES), "parameters": ["cwd"]}}
            assert method == "session.create"
            created.append(CanonicalCreateParams.model_validate(params))
            return {"stored_session_id": "stored"}

    @asynccontextmanager
    async def connected():
        yield Peer()

    async def rendered(self, query, *, oneshot):
        assert query == "x" and oneshot
        return 0

    monkeypatch.setattr(gateway_chat, "connect_gateway", connected)
    monkeypatch.setattr(GatewayChatView, "run", rendered)
    args = build_top_level_parser()[0].parse_args(["chat", "-q", "x", "--oneshot", "-Q", "--source", source])
    assert asyncio.run(gateway_chat.run_gateway_chat(args)) == 0
    assert len(created) == 1
    assert created[0].source == ("gui" if source == "desktop" else source)


def test_unknown_source_names_flag_and_choices_before_startup(capsys):
    parser = build_top_level_parser()[0]
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["chat", "-q", "x", "--oneshot", "-Q", "--source", "smoke-test"])
    error = capsys.readouterr().err
    assert refused.value.code == 2
    assert "argument --source" in error and "smoke-test" in error
    assert "choose from" in error
    for source in ("cli", "tui", "gui", "desktop", "acp", "tool", "oneshot"):
        assert repr(source) in error
    assert "model connection" not in error and "hermes doctor" not in error
