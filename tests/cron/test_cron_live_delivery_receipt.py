"""The live-adapter lane persists a per-run delivery receipt.

A successful live send used to leave only a log line: the platform's message id was never written
to the job, so "the breakdown went out" could not be checked once the log rotated. The receipt names
the run it belongs to, so a later run that delivers nothing can never borrow an earlier run's proof.
A receipt shows the platform accepted the message; it says nothing about whether anyone read it.
"""

import asyncio
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from cron import jobs
from cron.scheduler import _deliver_result
from gateway.config import Platform, PlatformConfig

CHAT_ID = "-1001234567890"
TARGET = f"telegram:{CHAT_ID}"


class _SendResult:
    def __init__(self, success=True, message_id=None):
        self.success = success
        self.message_id = message_id
        self.raw_response = None


def _deliver(job, send_result, standalone_result=None, *, for_failure=False):
    """Drive ``_deliver_result`` over the live lane against the real job store."""
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as e:
            future.set_exception(e)
        return future

    router = MagicMock()

    async def _deliver_to_platform(target, text, metadata, transport=None):
        return send_result

    router._deliver_to_platform = _deliver_to_platform

    async def _standalone(platform, pconfig, chat_id, text, **kwargs):
        return standalone_result if standalone_result is not None else {}

    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda p: None
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("gateway.delivery.DeliveryRouter", return_value=router), \
         patch("gateway.outbound.send._send_to_platform", _standalone), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        return _deliver_result(job, "Tonight's breakdown.", adapters={Platform.TELEGRAM: MagicMock()},
                               loop=loop, for_failure=for_failure)


def _saved(job_id):
    job = jobs.get_job(job_id)
    assert job is not None
    return job


def _run_of(job_id, execution_id, scheduled_for):
    """The in-memory job a scheduler run hands to delivery: the stored record plus its run identity."""
    return dict(_saved(job_id), execution_id=execution_id, _scheduled_instant=scheduled_for)


def _new_job(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return jobs.create_job(prompt="p", schedule="every 1d", deliver=TARGET)["id"]


def test_a_successful_send_stores_the_adapter_message_id_for_its_run(tmp_path, monkeypatch):
    job_id = _new_job(tmp_path, monkeypatch)

    error = _deliver(_run_of(job_id, "exec-mon", "2026-11-24T05:30:00+00:00"), _SendResult(message_id=4242))

    assert error is None
    receipt = _saved(job_id)["last_delivery_receipt"]
    assert receipt["execution_id"] == "exec-mon"
    assert receipt["scheduled_for"] == "2026-11-24T05:30:00+00:00"
    assert receipt["failure_notice"] is False
    assert list(receipt["targets"]) == [TARGET]
    assert receipt["targets"][TARGET]["message_id"] == "4242"
    assert receipt["targets"][TARGET]["at"]


def test_a_failed_send_after_a_success_leaves_the_earlier_receipt_naming_the_earlier_run(tmp_path, monkeypatch):
    """Yesterday delivered, today failed: after a reload, nothing reads as today's delivery."""
    job_id = _new_job(tmp_path, monkeypatch)
    assert _deliver(_run_of(job_id, "exec-mon", "2026-11-24T05:30:00+00:00"), _SendResult(message_id=4242)) is None
    jobs.mark_job_run(job_id, True)

    error = _deliver(_run_of(job_id, "exec-tue", "2026-11-25T05:30:00+00:00"),
                     _SendResult(success=False), standalone_result={"error": "chat not found"})
    jobs.mark_job_run(job_id, True, delivery_error=error)

    saved = _saved(job_id)
    assert error and "chat not found" in error
    assert saved["last_status"] == "delivery_failed"
    receipt = saved["last_delivery_receipt"]
    assert receipt["execution_id"] == "exec-mon"            # still yesterday's run...
    assert receipt["targets"][TARGET]["message_id"] == "4242"  # ...and yesterday's message
    assert receipt["execution_id"] != "exec-tue"            # today's run has no receipt


def test_a_standalone_fallback_success_records_no_live_receipt(tmp_path, monkeypatch):
    """Only the live lane is covered: a fallback send must not be mistaken for a live-adapter receipt."""
    job_id = _new_job(tmp_path, monkeypatch)

    error = _deliver(_run_of(job_id, "exec-mon", None), _SendResult(success=False),
                     standalone_result={"success": True, "message_id": "77"})

    assert error is None
    assert _saved(job_id).get("last_delivery_receipt") is None


def test_a_failure_notice_receipt_is_labelled_as_one(tmp_path, monkeypatch):
    job_id = _new_job(tmp_path, monkeypatch)

    _deliver(_run_of(job_id, "exec-mon", None), _SendResult(message_id=9), for_failure=True)

    assert _saved(job_id)["last_delivery_receipt"]["failure_notice"] is True
