"""Lineage SQL paging and removed-copy fallback share the display fold's semantics."""
import random

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


@pytest.mark.parametrize(('seed', 'latest', 'expected'), [
    (11, False, [11, 12, 16, 14, 15]),
    (16, False, [11, 12, 24, 20, 28]),
    (17, True, [28, 29, 30, 32, 33]),
])
def test_indexed_lineage_carrier_order_matches_upstream(tmp_path, seed, latest, expected):
    """A hidden uid original must not move a visible carrier ahead of its visible copy."""
    rng = random.Random(seed)
    with SessionDB(tmp_path / 'state.db') as db:
        originals = []
        for part in range(3):
            sid = f's{part}'
            db.create_session(sid, 'cli', parent_session_id=f's{part - 1}' if part else None)
            for n in range(15):
                message = {'role': 'assistant' if n % 2 else 'user',
                           'content': f'synthetic-{part}-{n}', 'timestamp': part * 20 + n}
                if originals and n % 4 == 0:
                    message = {k: v for k, v in rng.choice(originals).items()
                               if k in ('role', 'content', 'timestamp', 'message_uid')}
                    if n % 8 == 0:
                        message.update(content='synthetic carrier', _compressed_summary=True)
                if n == 7:
                    message['display_metadata'] = {MODEL_ONLY_DISPLAY_METADATA_KEY: True}
                db.append_messages_batch(sid, [message])
                originals.append(message)
                if part < 2:
                    db._write_sql('UPDATE messages SET active=0, compacted=1 WHERE id=?',
                                  (message['_row_id'],))
                elif seed % 2 and n % 6 == 0:
                    db._write_sql('UPDATE messages SET active=0, compacted=0 WHERE id=?',
                                  (message['_row_id'],))
            if part < 2:
                db._write_sql("UPDATE sessions SET ended_at=1, end_reason='compression' WHERE id=?", (sid,))

        db._write_sql('UPDATE messages SET display_identity=NULL, display_order=NULL')
        for part in range(3):
            assert db._ensure_display_order(f's{part}')
        rows = db.get_messages('s2', include_ancestors=True, include_compacted=True,
                               latest=latest, offset=8, limit=5)
        # These page IDs come from upstream's body-recomputed fold, not stored identities.
        assert [row['id'] for row in rows] == expected
