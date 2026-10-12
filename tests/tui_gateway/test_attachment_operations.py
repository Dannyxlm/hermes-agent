"""Identified attachment writes cannot leak into another surface's implicit next turn."""

import base64
import contextlib
import io
import os
import shutil
import subprocess
import threading
import types
from pathlib import Path

import pytest

from tui_gateway import server


@pytest.fixture
def session(tmp_path, monkeypatch):
    record = {"profile_home": str(tmp_path), "session_key": "conversation",
              "cwd": str(tmp_path), "attached_images": [], "running": False,
              "history": [], "history_lock": threading.RLock(), "transport": None}
    monkeypatch.setattr(server, "_sessions", {"live": record})
    monkeypatch.setattr(server, "_start_agent_build", lambda *_: None)
    return record


def call(name, **params):
    return server.handle_request({"id": "request", "method": name,
                                  "params": {"session_id": "live", **params}})


def upload(operation_id="image-operation", data=b"image bytes"):
    return call("image.attach_bytes", operation_id=operation_id, filename="shot.png",
                content_base64=base64.b64encode(data).decode())


def test_lost_reply_retry_returns_same_image_without_implicit_queue(session):
    first = upload()
    assert "error" not in first, first
    second = upload()
    assert second == first
    assert Path(first["result"]["path"]).read_bytes() == b"image bytes"
    assert session["attached_images"] == []
    status = call("attachment.operation.status", operation_id="image-operation")["result"]
    assert status["state"] == "staged"
    assert status["image_paths"] == [first["result"]["path"]]


def test_real_pdf_pages_survive_runtime_recreation_and_detach_without_other_images(session):
    if shutil.which("pdftoppm") is None:
        pytest.skip("Poppler is required for real PDF acceptance")
    from PIL import Image
    pdf = io.BytesIO()
    Image.new("RGB", (48, 48), "red").save(
        pdf, "PDF", save_all=True, append_images=[Image.new("RGB", (48, 48), "blue")])
    params = {"operation_id": "real-pdf", "filename": "two-pages.pdf",
              "content_base64": base64.b64encode(pdf.getvalue()).decode()}
    result = call("pdf.attach", **params)
    assert "error" not in result, result
    pages = result["result"]["pages"]
    assert [page["page"] for page in pages] == [1, 2]
    paths = [Path(page["path"]) for page in pages]
    assert all(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for path in paths)
    assert call("pdf.attach", **params) == result
    desktop = Path(session["profile_home"]) / "desktop.png"
    desktop.write_bytes(b"other surface's original")
    server._sessions["live"] = {**session, "attached_images": [str(desktop)]}
    status = call("attachment.operation.status", operation_id="real-pdf")["result"]
    assert status["state"] == "staged"
    assert status["image_paths"] == [str(path) for path in paths]
    assert call("attachment.operation.detach", operation_id="real-pdf")["result"]["state"] == "detached"
    assert all(not path.exists() for path in paths)
    assert desktop.read_bytes() == b"other surface's original"
    assert server._sessions["live"]["attached_images"] == [str(desktop)]


def test_cleanup_tombstone_fences_delayed_upload_and_other_queue(session):
    session["attached_images"] = ["desktop-image"]
    detached = call("attachment.operation.detach", operation_id="cancelled")
    assert "error" not in detached, detached
    assert detached["result"]["state"] == "detached"
    assert upload("cancelled")["error"]["code"] == 4020
    assert session["attached_images"] == ["desktop-image"]


def test_explicit_empty_busy_selection_cannot_consume_desktop_queue(session, monkeypatch):
    session.update(running=True, agent=types.SimpleNamespace(), attached_images=["desktop-image"])
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    monkeypatch.setattr(server, "_persist_queued_user_row", lambda *_: None)
    result = server._handle_busy_submit("request", "live", session, "mobile text", None,
                                        queued=True, image_paths=[])
    assert result["result"]["status"] == "queued"
    assert session["attached_images"] == ["desktop-image"]
    assert session["queued_prompt"]["image_paths"] == []


def test_explicit_selection_is_a_declared_shared_wire_contract():
    from tui_gateway.contracts import METHODS
    for name, scope in (("prompt.submit", {"session_id": "live"}),
                        ("mobile.submit", {"session_id": "live", "profile": "ava",
                                           "canonical_root_id": "root"})):
        model = METHODS[name].params.model_validate({**scope, "text": "hello", "image_paths": [],
                                                   "attachment_operation_ids": []})
        assert model.image_paths == []


def test_partial_pdf_is_durable_and_cleanup_owns_only_its_pages(session, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/test/pdftoppm")

    def render(argv, **_):
        for page in (1, 2):
            Path(f"{argv[-1]}-{page}.png").write_bytes(f"page {page}".encode())
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", render)
    real_open = os.open

    def fail_second_page(path, *args, **kwargs):
        if Path(path).name == "image-1.png":
            raise OSError("interrupted PDF write")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_second_page)
    params = {"operation_id": "pdf-operation", "content_base64": base64.b64encode(b"%PDF-test").decode()}
    result = call("pdf.attach", **params)
    assert result["error"]["code"] == 5027
    # Recreate the runtime record: nothing from the staging process is needed for reconciliation.
    server._sessions["live"] = {**session, "attached_images": ["desktop-image"]}
    status = call("attachment.operation.status", operation_id="pdf-operation")["result"]
    assert status["state"] == "partial"
    assert status["completed_images"] == 1
    assert Path(status["image_paths"][0]).read_bytes() == b"page 1"
    assert call("pdf.attach", **params)["error"]["code"] == 4020
    assert call("attachment.operation.detach", operation_id="pdf-operation")["result"]["state"] == "detached"
    assert all(not Path(path).exists() for path in status["image_paths"])
    assert server._sessions["live"]["attached_images"] == ["desktop-image"]


def test_failed_intent_persistence_writes_no_image_bytes(session, monkeypatch):
    from tui_gateway.attachment_operations import AttachmentOperations

    def fail(*_):
        raise OSError("intent disk full")

    monkeypatch.setattr(AttachmentOperations, "write", fail)
    assert upload()["error"]["code"] == 5027
    assert list(Path(session["profile_home"]).rglob("*.png")) == []
    assert session["attached_images"] == []


def test_operations_cannot_be_read_or_cancelled_from_another_conversation(session):
    result = upload()["result"]
    server._sessions["live"] = {**session, "session_key": "different-conversation"}
    status = call("attachment.operation.status", operation_id="image-operation")["result"]
    assert status["state"] == "missing"
    assert call("attachment.operation.detach", operation_id="image-operation")["result"]["state"] == "detached"
    assert Path(result["path"]).read_bytes() == b"image bytes"
    server._sessions["live"] = session
    assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == "staged"


def test_foreign_transport_and_profile_cannot_access_operations(session, monkeypatch, tmp_path):
    upload()
    monkeypatch.setattr(server, "current_transport", lambda: object())
    monkeypatch.setattr(server, "_session_transport_contains", lambda *_: False)
    for name in ("attachment.operation.status", "attachment.operation.detach"):
        assert call(name, operation_id="image-operation")["error"]["code"] == 4017
    assert upload("another")["error"]["code"] == 4017
    monkeypatch.setattr(server, "current_transport", lambda: None)
    monkeypatch.setattr(server, "_profile_home", lambda *_: tmp_path / "other-profile")
    assert call("attachment.operation.status", operation_id="image-operation", profile="other")["error"]["code"] == 4017


def test_changed_bytes_cannot_reuse_an_operation_identity(session):
    first = upload()["result"]
    assert upload(data=b"different bytes")["error"]["code"] == 4020
    assert Path(first["path"]).read_bytes() == b"image bytes"


def test_busy_dispatch_selects_only_own_operations_and_receipt_is_durable(session, monkeypatch):
    staged = upload()["result"]
    session.update(running=True, agent=types.SimpleNamespace(), attached_images=["desktop-image"])
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_: None)
    monkeypatch.setattr(server, "_reattach_refusal", lambda *_: None)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    monkeypatch.setattr(server, "_persist_queued_user_row", lambda *_: None)
    response = call("prompt.submit", text="inspect", image_paths=[],
                    attachment_operation_ids=["image-operation"], queued=True)
    assert response["result"]["status"] == "queued"
    assert session["queued_prompt"]["image_paths"] == [staged["path"]]
    assert session["attached_images"] == ["desktop-image"]
    server._sessions["live"] = {**session}
    status = call("attachment.operation.status", operation_id="image-operation")["result"]
    assert status["state"] == "submitted"
    assert status["submission_result"]["status"] == "queued"
    assert call("attachment.operation.detach", operation_id="image-operation")["error"]["code"] == 4020
    assert call("prompt.submit", text="retry", attachment_operation_ids=["image-operation"])["error"]["code"] == 4020


def test_explicit_paths_cannot_read_arbitrary_gateway_files(session, tmp_path):
    path = tmp_path / "private.png"
    path.write_bytes(b"private")
    response = call("prompt.submit", text="inspect", image_paths=[str(path)])
    assert response["error"]["code"] == 4017
    assert session["running"] is False


def test_staging_lock_blocks_cleanup_but_process_recreation_allows_it(session):
    from tui_gateway.attachment_operations import AttachmentOperations
    store = server._attachment_operation_store(session, {"session_id": "live"})
    with store.locked("active-pdf"):
        store.write("active-pdf", {"operation_id": "active-pdf", "state": "staging", "image_paths": []})
        assert call("attachment.operation.detach", operation_id="active-pdf")["error"]["code"] == 4020
    # The OS releases this lock on process death; the next store can reconcile the persisted intent.
    recreated = AttachmentOperations(server._session_images_dir(session), "conversation")
    assert recreated.detach("active-pdf")["state"] == "detached"


def test_failed_detach_keeps_debt_and_can_reconcile_without_other_operations(session, monkeypatch):
    first = upload("first")["result"]
    other = upload("other")["result"]
    real_unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if str(path) == first["path"]:
            raise OSError("detach interrupted")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)
    assert call("attachment.operation.detach", operation_id="first")["error"]["code"] == 5027
    assert call("attachment.operation.status", operation_id="first")["result"]["state"] == "detaching"
    assert Path(other["path"]).read_bytes() == b"image bytes"
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert call("attachment.operation.detach", operation_id="first")["result"]["state"] == "detached"
    assert Path(other["path"]).exists()


def test_selection_preserves_composer_order_and_explicit_empty_on_drain(session, monkeypatch):
    first = upload("z-first")["result"]
    second = upload("a-second")["result"]
    store = server._attachment_operation_store(session, {"session_id": "live"})
    with store.selection(["z-first", "a-second"]) as receipts:
        assert [r["image_paths"][0] for r in receipts] == [first["path"], second["path"]]
    session.update(queued_prompt={"text": "no images", "image_paths": [], "transport": None},
                   agent=types.SimpleNamespace(), attached_images=["desktop-image"])
    dispatched = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *args, **kwargs: dispatched.append(kwargs))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_: False)
    assert server._drain_queued_prompt("request", "live", session) is True
    assert dispatched[0]["image_paths"] == []
    assert session["attached_images"] == ["desktop-image"]


def test_receipt_scope_follows_compression_root_but_not_branches(session):
    from hermes_state import SessionDB
    with SessionDB(db_path=Path(session["profile_home"]) / "state.db") as db:
        db.create_session("conversation", "desktop")
        db._conn.execute("UPDATE sessions SET end_reason='compression' WHERE id='conversation'")
        db.create_session("continuation", "desktop", parent_session_id="conversation")
    first = upload()["result"]
    session["session_key"] = "continuation"
    status = call("attachment.operation.status", operation_id="image-operation")["result"]
    assert status["state"] == "staged"
    assert status["image_paths"] == [first["path"]]


@pytest.mark.parametrize("key", ["image_paths", "attachment_operation_ids"])
@pytest.mark.parametrize("value", ["", False, 0, {}])
def test_malformed_selection_is_rejected_before_submit(session, key, value):
    assert call("prompt.submit", text="should not send", **{key: value})["error"]["code"] == 4017
    assert session["running"] is False


def test_runtime_replacement_during_selection_cannot_dispatch_old_files(session, monkeypatch):
    from tui_gateway.attachment_operations import AttachmentOperations
    upload()
    selection = AttachmentOperations.selection

    @contextlib.contextmanager
    def replace_runtime(store, operations):
        with selection(store, operations) as receipts:
            server._sessions["live"] = {**session, "session_key": "new-owner", "attached_images": []}
            yield receipts

    monkeypatch.setattr(AttachmentOperations, "selection", replace_runtime)
    response = call("prompt.submit", text="old owner", attachment_operation_ids=["image-operation"])
    assert response["error"]["code"] == 4001
    assert server._sessions["live"]["running"] is False
    server._sessions["live"] = session
    assert call("attachment.operation.status", operation_id="image-operation")["result"]["state"] == "staged"



