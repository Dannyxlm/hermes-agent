"""Combined list totals retain the existing count contract across archive and source scopes."""
import json

import pytest

from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX
from hermes_state_listing_counts import listing_counts


@pytest.mark.parametrize('archive', ['exclude', 'only', 'include'])
@pytest.mark.parametrize('filters', [{}, {'exclude_sources': ['telegram']}, {'sources': ['tui']},
                                      {'cwd_prefix': '/fixture/a_b'}, {'min_message_count': 1}])
def test_combined_totals_equal_independent_scoped_counts(tmp_path, archive, filters):
    with SessionDB(tmp_path / 'state.db') as db:
        for sid, source, cwd in [('visible', 'tui', '/fixture/a_b'), ('archived', 'tui', '/fixture/a_b/sub'),
                                 ('other', 'telegram', '/fixture/axb'), ('empty', 'tui', '/fixture/else')]:
            db.create_session(sid, source=source, cwd=cwd)
            if sid != 'empty':
                db.append_message(sid, 'user', 'fictional fixture')
        db.set_session_archived('archived', True)
        db.end_session('visible', 'compression')
        db.create_session('tip', source='tui', parent_session_id='visible')
        db.append_message('tip', 'assistant', 'fictional continuation')
        db.create_session('child', source='subagent', parent_session_id='other',
                          model_config={'_delegate_from': 'other'})
        db.create_session('reset-owner', source='tui', cwd='/fixture/a_b')
        db.end_session('reset-owner', 'session_reset')
        db.create_session('reset-next', source='tui', cwd='/fixture/a_b')
        db.append_message('reset-owner', 'user', 'fictional reset predecessor')
        db.append_message('reset-next', 'user', 'fictional reset successor')
        db.set_meta(POLICY_PREFIX + 'reset-owner', json.dumps({
            'session_id': 'reset-owner', 'lineage': ['reset-owner', 'reset-next']}))
        db.create_session('imported-archive', source='tui', cwd='/fixture/a_b')
        db.append_message('imported-archive', 'user', 'fictional imported row')
        db._execute_write(lambda conn: conn.execute(
            'UPDATE sessions SET archived=2 WHERE id=?', ('imported-archive',)))
        scope = dict(exclude_children=True, include_archived=archive == 'include',
                     archived_only=archive == 'only', **filters)
        expected = db.session_count(**scope)
        archived = db.session_count(**{**scope, 'include_archived': False, 'archived_only': True})
        assert listing_counts(db, **scope) == (expected, archived)
        db.set_session_archived('archived', False)
        assert listing_counts(db, **scope) == (
            db.session_count(**scope),
            db.session_count(**{**scope, 'include_archived': False, 'archived_only': True}))
