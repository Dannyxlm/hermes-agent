"""Process-local, metadata-only side-task observations, independent of live sessions."""
from pathlib import Path
import threading
import time

from tui_gateway.event_replay import replay_epoch


class SideTaskRegistry:
    def __init__(self, *, epoch, clock=time.time):
        self.epoch = epoch
        self._clock = clock
        self._lock = threading.Lock()
        self._records = {}

    def _prune(self):
        # Caller holds the lock. Running records have no age/count eviction.
        cutoff = self._clock() - 6 * 3600
        terminal = {}
        for key, row in list(self._records.items()):
            if row['status'] == 'running':
                continue
            if row['finished_at'] < cutoff:
                del self._records[key]
            else:
                terminal.setdefault(key[:2], []).append((key, row['finished_at']))
        for entries in terminal.values():
            for key, _ in sorted(entries, key=lambda item: item[1], reverse=True)[50:]:
                del self._records[key]

    def register(self, profile_home, parent_id, task_id, kind):
        if kind not in ('background', 'btw'):
            raise ValueError('Unsupported side-task kind')
        key = (str(Path(profile_home).resolve()), parent_id, task_id, self.epoch)
        with self._lock:
            self._prune()
            self._records[key] = {'task_id': task_id, 'kind': kind,
                                  'status': 'running', 'started_at': self._clock()}
        return key

    def finish(self, key, status):
        if status not in ('completed', 'failed', 'failed_start'):
            raise ValueError('Unsupported side-task status')
        with self._lock:
            self._prune()
            row = self._records.get(key)
            if row is not None and row['status'] == 'running':
                row.update(status=status, finished_at=self._clock())

    def snapshot(self, profile_home, parent_ids):
        home = str(Path(profile_home).resolve())
        parents = set(parent_ids)
        with self._lock:
            self._prune()
            return [dict(row) for key, row in self._records.items()
                    if key[0] == home and key[1] in parents and key[3] == self.epoch]


registry = SideTaskRegistry(epoch=replay_epoch())
