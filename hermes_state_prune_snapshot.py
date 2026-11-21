"""Read-side metadata projection for a bounded prune transaction."""
from collections import defaultdict
import json

from hermes_state_common import LEGACY_QUEUE_RECEIPT_PREFIX
from hermes_state_keys import prefix_range
from hermes_state_local import API_DECLARED_PREFIX
from hermes_state_mutation_retirement import (
    _MUTATION_TRANSCRIPT_FIELDS, _RECEIPT_SESSION_PATHS, entry_session_id,
)
from hermes_state_runtime import _json


VERSION_KEY = 'maintenance.prune.receipts_version.v1'


def receipts_version(conn):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (VERSION_KEY,)).fetchone()
    return row[0] if row else '0'


class PruneSnapshot:
    """Decode broad receipt/route prefixes once; the writer only revisits selected keys."""

    def __init__(self, conn, candidates):
        self.metadata = defaultdict(list)
        self.routes = defaultdict(list)
        for key, raw in conn.execute('SELECT key,value FROM state_meta WHERE key>=? AND key<?',
                                     prefix_range('gateway.mutation.v1.')):
            try:
                receipt = json.loads(raw)
            except (TypeError, ValueError):
                continue
            result = receipt.get('result') if isinstance(receipt, dict) else None
            if not isinstance(result, dict) or not any(k in result for k in _MUTATION_TRANSCRIPT_FIELDS):
                continue
            targets = set()
            for path in _RECEIPT_SESSION_PATHS:
                value = receipt
                for part in path.split('.')[1:]:
                    value = value.get(part) if isinstance(value, dict) else None
                if isinstance(value, str) and value in candidates:
                    targets.add(value)
            if targets:
                receipt['result'] = {k: None if k in _MUTATION_TRANSCRIPT_FIELDS else v for k, v in result.items()}
                compact = _json(receipt)
                for sid in targets:
                    self.metadata[sid].append((key, raw, compact))
        for prefix in (API_DECLARED_PREFIX, LEGACY_QUEUE_RECEIPT_PREFIX):
            for key, raw in conn.execute('SELECT key,value FROM state_meta WHERE key>=? AND key<?',
                                         prefix_range(prefix)):
                target = raw if prefix == API_DECLARED_PREFIX else entry_session_id(raw)
                if isinstance(target, str) and target in candidates:
                    self.metadata[target].append((key, raw, None))
        for scope, key, raw in conn.execute('SELECT scope,session_key,entry_json FROM gateway_routing'):
            target = entry_session_id(raw)
            if isinstance(target, str) and target in candidates:
                self.routes[target].append((scope, key, raw))

    def retire_metadata(self, conn, ids):
        for sid in ids:
            for key, raw, compact in self.metadata.get(sid, ()):
                if compact is None:
                    conn.execute('DELETE FROM state_meta WHERE key=? AND value=?', (key, raw))
                else:
                    conn.execute('UPDATE state_meta SET value=? WHERE key=? AND value=?', (compact, key, raw))

    def retire_routes(self, conn, ids):
        for sid in ids:
            for scope, key, raw in self.routes.get(sid, ()):
                conn.execute('DELETE FROM gateway_routing WHERE scope=? AND session_key=? AND entry_json=?',
                             (scope, key, raw))
