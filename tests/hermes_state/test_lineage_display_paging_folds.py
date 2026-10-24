"""Lineage SQL paging and removed-copy fallback share the display fold's semantics."""
import pytest

from agent.context_compressor import MODEL_ONLY_DISPLAY_METADATA_KEY
from hermes_state import SessionDB


@pytest.mark.parametrize('removed', [False, True])
@pytest.mark.parametrize('indexed', [False, True])
def test_lineage_pages_fold_summary_carriers_removed_copies_and_model_only(tmp_path, removed, indexed):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('root', 'cli')
        originals = [{'role': 'user', 'content': 'first', 'timestamp': 1},
                     {'role': 'assistant', 'content': 'original reply', 'timestamp': 2}]
        db.append_messages_batch('root', originals)
        db._write_sql('UPDATE messages SET active=0, compacted=1')
        db.create_session('child', 'cli', parent_session_id='root')
        db._write_sql("UPDATE sessions SET end_reason='compression', ended_at=3 WHERE id='root'")
        carrier = {k: v for k, v in originals[1].items() if k in ('role', 'timestamp', 'message_uid')}
        carrier.update(content='wrapped reply and summary', _compressed_summary=True)
        tail = [carrier, {'role': 'user', 'content': 'latest', 'timestamp': 4},
                {'role': 'assistant', 'content': 'hidden model context', 'timestamp': 5,
                 'display_metadata': {MODEL_ONLY_DISPLAY_METADATA_KEY: True}}]
        db.append_messages_batch('child', tail)
        if removed:
            copied = {k: v for k, v in originals[0].items()
                      if k in ('role', 'content', 'timestamp', 'message_uid')}
            db.append_messages_batch('child', [copied])
            db._write_sql('UPDATE messages SET active=0, compacted=0 WHERE id=?', (copied['_row_id'],))
        if not indexed:
            db._write_sql('UPDATE messages SET display_identity=NULL, display_order=NULL')
        expected = ([] if removed else ['first']) + ['wrapped reply and summary', 'latest']
        shown = db.get_messages('child', include_ancestors=True, include_compacted=True)
        assert [row['content'] for row in shown] == expected
        for offset in range(len(expected)):
            page = db.get_messages('child', include_ancestors=True, include_compacted=True,
                                   latest=True, limit=1, offset=offset)
            assert [row['content'] for row in page] == [expected[-offset - 1]]
