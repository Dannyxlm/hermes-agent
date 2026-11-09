"""v0.23 rollback fence: Group Chat clients cannot choose 'auto' routing yet.

The release's rollback target plans only 'everyone' and 'lead' rooms; a stored 'auto' would stall the
room there. So the canonical groups.context write refuses it, the driver status stops offering it, and
the other routing modes still save (state probe: outputs/releases/2026-11-08-hermes-v0.23/state).
"""
import asyncio
import json
from pathlib import Path

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket


def test_groups_context_refuses_auto_routing_and_keeps_the_rest(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    (home / 'profiles' / 'helper').mkdir(parents=True)
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'hosted_rooms': {'profiles': {'helper': str(home / 'profiles' / 'helper')}},
        'model': {'provider': 'custom', 'default': 'no-inference', 'base_url': 'http://127.0.0.1:1/v1'},
        'auxiliary': {'title_generation': {'enabled': False}},
    }))
    env = child_env() | dict(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
                             PYTHONPATH=str(root), PYTHONUNBUFFERED='1')

    async def probe(desc):
        async with websocket(home, desc) as ws:
            members = [{'member_id': 'one', 'profile': 'default', 'handle': 'one'},
                       {'member_id': 'two', 'profile': 'helper', 'handle': 'two'}]
            created = await rpc(ws, 'groups.create', room_id='room-fence', name='Fence', members=members)
            assert 'result' in created, created

            refused = await rpc(ws, 'groups.context', room_id='room-fence', event_id='auto',
                                purpose='Ship', notes='', routing='auto')
            assert 'error' in refused, refused
            state = (await rpc(ws, 'groups.state', room_id='room-fence'))['result']
            assert state['context']['routing'] == 'everyone'
            assert state['context']['purpose'] == ''
            if 'driver_status' in state:
                assert state['driver_status']['routing_modes'] == ['everyone', 'lead']

            lead = await rpc(ws, 'groups.context', room_id='room-fence', event_id='lead',
                             purpose='Ship', notes='', routing='lead', lead_member_id='one')
            assert lead['result']['context']['routing'] == 'lead', lead
            kept = await rpc(ws, 'groups.context', room_id='room-fence', event_id='purpose',
                             purpose='Ship it', notes='')
            assert kept['result']['context']['routing'] == 'lead', kept
            back = await rpc(ws, 'groups.context', room_id='room-fence', event_id='everyone',
                             purpose='Ship it', notes='', routing='everyone')
            assert back['result']['context']['routing'] == 'everyone', back

    with daemon(root, home, env, barrier=False) as (_, desc):
        asyncio.run(probe(desc))
