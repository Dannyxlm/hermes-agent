"""Durable image/PDF staging receipts, outside the legacy implicit attachment queue.

One operation names one upload. Its intent and planned paths are durable before
bytes are written, so a lost reply or interrupted PDF is recoverable after restart.
The profile/lineage and transport are admitted by the RPC layer, never by this store.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path

from hermes_platform.host.facts import os_family
from utils import atomic_json_write


def _sync_directory(path: Path) -> None:
    """Operation fences require directory durability wherever directory fsync is supported."""
    if os_family() == "win32":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class OperationConflict(ValueError):
    """A busy, incomplete, detached or already-dispatched operation cannot be replayed."""


class AttachmentOperations:
    def __init__(self, images_root: Path, session_root: str):
        self.root = images_root / "operations" / hashlib.sha256(session_root.encode()).hexdigest()

    def directory(self, operation_id: str) -> Path:
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id):
            raise ValueError("operation_id must be 1–128 letters, digits, underscores or hyphens")
        return self.root / operation_id

    @contextlib.contextmanager
    def locked(self, operation_id: str):
        from gateway.status import _release_file_lock, _try_acquire_file_lock
        directory = self.directory(operation_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Sync ancestors even on retry: a refused first write may have left directories
        # present in memory without their parent entries reaching durable storage.
        for parent in directory.parents:
            _sync_directory(parent)
        fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+") as handle:
            if not _try_acquire_file_lock(handle):
                raise OperationConflict("attachment operation is busy; reconcile after it finishes")
            try:
                yield directory
            finally:
                _release_file_lock(handle)

    def read(self, operation_id: str) -> dict:
        path = self.directory(operation_id) / "receipt.json"
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return {"operation_id": operation_id, "state": "missing", "image_paths": []}

    def write(self, operation_id: str, receipt: dict):
        atomic_json_write(self.directory(operation_id) / "receipt.json", receipt,
                          mode=0o600)
        _sync_directory(self.directory(operation_id))

    def stage(self, operation_id: str, fingerprint: str, files: list[tuple[bytes | Path, str]], result):
        """``result(paths)`` builds the existing attach reply; each planned file belongs to this op."""
        with self.locked(operation_id) as directory:
            receipt = self.read(operation_id)
            if receipt["state"] != "missing":
                if receipt.get("fingerprint") != fingerprint:
                    raise OperationConflict("operation_id was already used for a different upload")
                if receipt["state"] == "staged":
                    return receipt["result"]
                raise OperationConflict("attachment operation is not staged; reconcile before continuing")
            paths = [str(directory / f"image-{i}{ext}") for i, (_, ext) in enumerate(files)]
            receipt = {"operation_id": operation_id, "state": "staging", "image_paths": paths,
                       "fingerprint": fingerprint, "completed_images": 0}
            self.write(operation_id, receipt)
            try:
                for data, path in zip((data for data, _ in files), paths):
                    if isinstance(data, Path):
                        data = data.read_bytes()
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    _sync_directory(directory)
                    receipt["completed_images"] += 1
                    self.write(operation_id, receipt)
                receipt["result"] = {**result(paths), "operation_id": operation_id}
                receipt["state"] = "staged"
                self.write(operation_id, receipt)
            except Exception:
                receipt["state"] = "partial"
                self.write(operation_id, receipt)
                raise
            return receipt["result"]

    def detach(self, operation_id: str) -> dict:
        with self.locked(operation_id):
            receipt = self.read(operation_id)
            if receipt["state"] in {"dispatching", "submitted"}:
                raise OperationConflict("attachment operation was dispatched; retain it for send reconciliation")
            # The tombstone fences a delayed original request, even if it had not begun staging.
            receipt["state"] = "detaching"
            self.write(operation_id, receipt)
            for path in receipt["image_paths"]:
                Path(path).unlink(missing_ok=True)
            receipt["state"] = "detached"
            self.write(operation_id, receipt)
            return self.public(receipt)

    @staticmethod
    def public(receipt: dict) -> dict:
        return {key: receipt[key] for key in
                ("operation_id", "state", "image_paths", "completed_images", "result", "submission_result")
                if key in receipt}

    @contextlib.contextmanager
    def selection(self, operation_ids: list[str]):
        """Hold selected operations through RPC acceptance; uncertain dispatch cannot be replayed."""
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("attachment_operation_ids contains duplicates")
        with contextlib.ExitStack() as stack:
            receipts = {}
            for operation_id in sorted(operation_ids):
                stack.enter_context(self.locked(operation_id))
                receipt = self.read(operation_id)
                if receipt["state"] != "staged":
                    raise OperationConflict("selected attachment operation is not completely staged")
                if any(not Path(path).is_file() for path in receipt["image_paths"]):
                    raise OperationConflict("staged image is unavailable; retain the original and reconcile")
                receipts[operation_id] = receipt
            yield [receipts[operation_id] for operation_id in operation_ids]

    def dispatch(self, receipts: list[dict]):
        for receipt in receipts:
            receipt["state"] = "dispatching"
            receipt.pop("queue", None)
            self.write(receipt["operation_id"], receipt)

    def queued(self, operation_ids: list[str], queue_id: str, sid: str):
        """Receipt locks are held by submit; the queue owner's history lock binds the envelope."""
        from tui_gateway.turn_marker import _writer_identity
        for operation_id in operation_ids:
            receipt = self.read(operation_id)
            receipt["queue"] = {"id": queue_id, "sid": sid, "phase": "queued", **_writer_identity()}
            self.write(operation_id, receipt)

    def begin_queued_dispatch(self, operation_ids: list[str], queue_id: str):
        """Fence dispatch under receipt locks before the queue owner removes its envelope."""
        for operation_id in operation_ids:
            with self.locked(operation_id):
                receipt = self.read(operation_id)
                if receipt.get("queue", {}).get("id") != queue_id:
                    raise OperationConflict("queued attachment ownership changed; reconcile before dispatch")
                receipt["queue"]["phase"] = "dispatching"
                self.write(operation_id, receipt)

    def reconcile_queue(self, operation_id: str, live_queue_ids: set[str], sid: str, *, cancelled_id: str = "",
                        refused_id: str = ""):
        from tui_gateway.turn_marker import marker_writer_state
        receipt = self.read(operation_id)
        if not receipt.get("queue"):
            return receipt
        with self.locked(operation_id):
            receipt = self.read(operation_id)
            queued = receipt.get("queue")
            if not queued or queued["id"] in live_queue_ids:
                return receipt
            expected_id = cancelled_id or refused_id
            if expected_id and queued["id"] != expected_id:
                return receipt
            if queued.get("phase") != "queued" and queued["id"] not in {cancelled_id, refused_id}:
                return receipt
            local_owner = queued.get("writer_pid") == os.getpid() and queued.get("sid") == sid
            if queued["id"] not in {cancelled_id, refused_id} and not local_owner and marker_writer_state(queued) != "dead":
                return receipt
            receipt.pop("queue", None)
            receipt.pop("submission_result", None)
            receipt["state"] = "staged"
            self.write(operation_id, receipt)
            return receipt

    def accepted(self, receipts: list[dict], result: dict):
        for receipt in receipts:
            # A fast queue drain may already have fenced dispatch. Never replace its
            # newer queue metadata with the submit wrapper's acceptance-time copy.
            receipt = self.read(receipt["operation_id"])
            if receipt["state"] == "staged":
                continue  # Stop or a proven drain refusal already retired this queue acceptance.
            receipt["state"] = "submitted"
            receipt["submission_result"] = result
            self.write(receipt["operation_id"], receipt)

    def restore_staged(self, receipts: list[dict]):
        """Only a known refusal before queue/worker dispatch can release the fence."""
        for receipt in receipts:
            receipt["state"] = "staged"
            receipt.pop("queue", None)
            self.write(receipt["operation_id"], receipt)
