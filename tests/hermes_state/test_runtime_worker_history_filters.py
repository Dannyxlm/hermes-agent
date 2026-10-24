"""Managed-worker replay filters must preserve the owner's row selection."""
import pytest


@pytest.mark.parametrize('include_compacted', [False, True])
def test_worker_history_applies_inactive_and_uid_filters(worker, include_compacted):
    db, store = worker
    messages = [{'role': 'user', 'content': 'older'},
                {'role': 'assistant', 'content': 'exclude'},
                {'role': 'user', 'content': 'current'}]
    store.append_messages_batch('owned', messages)
    db._write_sql('UPDATE messages SET active=0 WHERE session_id=? AND id<?',
                  ('owned', messages[-1]['_row_id']))
    excluded = db.get_messages('owned', include_inactive=True)[1]['message_uid']
    kwargs = dict(only_inactive=True, exclude_message_uid=excluded,
                  include_compacted=include_compacted, include_row_ids=True)
    expected = db.get_messages_as_conversation('owned', **kwargs)
    assert [m['content'] for m in expected] == ['older']
    assert store.get_messages_as_conversation('owned', **kwargs) == expected
