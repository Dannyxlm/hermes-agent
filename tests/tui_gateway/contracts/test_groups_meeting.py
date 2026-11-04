"""Both gateway contracts accept the same optional, validated meeting metadata."""
import pytest
from pydantic import ValidationError

from tui_gateway.contracts.registry import CANONICAL_METHODS, METHODS, canonical_param_problems, validate_params


MEETING = {'kind': 'standup', 'schedule_local': '08:15', 'timezone': 'America/Edmonton'}
PARAMS = {'room_id': 'meeting-fixture', 'name': 'Meeting fixture', 'members': []}


@pytest.mark.parametrize('catalog', [METHODS, CANONICAL_METHODS], ids=['legacy', 'canonical'])
def test_meeting_create_contract_accepts_marker_and_ordinary_room(catalog):
    contract = catalog['groups.create']
    assert contract.params.model_validate(PARAMS).meeting is None
    assert contract.params.model_validate({**PARAMS, 'meeting': None}).meeting is None
    result = contract.params.model_validate({**PARAMS, 'meeting': MEETING})
    assert result.meeting is not None
    assert result.meeting.model_dump() == MEETING
    _, error = validate_params(contract, {**PARAMS, 'meeting': MEETING})
    assert error is None


@pytest.mark.parametrize('catalog', [METHODS, CANONICAL_METHODS], ids=['legacy', 'canonical'])
@pytest.mark.parametrize('meeting', [
    {**MEETING, 'kind': 'other'}, {**MEETING, 'schedule_local': '24:00'},
    {**MEETING, 'timezone': 'Not/A_Timezone'}, {**MEETING, 'extra': 'unknown'},
])
def test_meeting_create_contract_rejects_malformed_marker(catalog, meeting):
    with pytest.raises(ValidationError):
        catalog['groups.create'].params.model_validate({**PARAMS, 'meeting': meeting})


def test_dispatcher_rejects_unknown_nested_meeting_fields():
    params = {**PARAMS, 'meeting': {**MEETING, 'extra': 'unknown'}}
    _, error = validate_params(METHODS['groups.create'], params)
    assert error is not None and 'meeting.extra' in error
    assert canonical_param_problems(CANONICAL_METHODS['groups.create'], params) == ['meeting.extra']
