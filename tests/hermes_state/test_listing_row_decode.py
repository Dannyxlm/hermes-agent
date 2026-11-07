"""Wide listing rows decode without repeated linear named-column lookups."""
import sqlite3

import pytest

from hermes_state import SessionDB


def test_wide_session_row_uses_positional_values_once():
    named = []
    class CountedRow(sqlite3.Row):
        def __getitem__(self, key):
            if isinstance(key, str):
                named.append(key)
            return super().__getitem__(key)
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = CountedRow
        row = conn.execute('SELECT ' + ','.join(f'{i} AS c{i}' for i in range(80))).fetchone()
    assert SessionDB._session_row_dict(row) == {f'c{i}': i for i in range(80)}
    assert named == []


@pytest.mark.parametrize('columns', ['1 AS id, 2 AS id', '1 AS id, 2 AS ID'])
def test_duplicate_column_names_keep_sqlite_first_match_semantics(columns):
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT ' + columns).fetchone()
    assert SessionDB._session_row_dict(row) == dict(row)


def test_plain_mapping_and_resolved_fields_keep_their_shape():
    row = {'id': 'example', 'system_prompt': None, '_system_prompt_resolved': 'resolved',
           'model_config': '{"_reset_from":" root "}'}
    expected = {'id': 'example', 'system_prompt': 'resolved', 'model_config': row['model_config'], '_reset_from': 'root'}
    assert SessionDB._session_row_dict(row) == expected
