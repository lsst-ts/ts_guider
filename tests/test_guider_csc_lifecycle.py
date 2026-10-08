# This file is part of ts_guider.
#
# Developed for the Vera C. Rubin Observatory Telescope and Site Systems.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

import asyncio
import importlib
import logging
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from lsst.ts import salobj
from lsst.ts.guider.error_code import ErrorCode

module = importlib.import_module("lsst.ts.guider.guider_csc")


@pytest.fixture
def csc(monkeypatch):
    """Initialize the real CSC lifecycle without salobj network resources."""
    monkeypatch.setattr(salobj.ConfigurableCsc, "__init__", lambda *a, **k: None)
    monkeypatch.setattr(salobj.ConfigurableCsc, "close_tasks", AsyncMock())
    csc = module.GuiderCsc()
    csc.log = logging.getLogger("LifecycleTest")
    csc.config = SimpleNamespace(partition="test")
    csc.evt_simulationMode = SimpleNamespace(data=SimpleNamespace(mode=1))
    csc._summary_state = salobj.State.ENABLED

    async def fault(**kwargs):
        csc._summary_state = salobj.State.FAULT
        await csc.handle_summary_state()

    csc.fault = AsyncMock(side_effect=fault)
    return csc


@pytest.fixture
def processor(monkeypatch):
    """Replace image processing so failures and shutdown can be controlled."""
    processor = Mock()
    processor._sensor_name = lambda index: f"sensor_{index}"
    processor.combiner.orientation.camera = object()
    factory = Mock(return_value=processor)
    monkeypatch.setattr(module, "StreamingGuiderProcessor", factory)
    processor.factory = factory
    return processor


def bind_source(monkeypatch, csc, source):
    """Provide a controllable native source without loading the SDK."""
    csc.evt_simulationMode.data.mode = 0
    binding = SimpleNamespace(
        DaqStampSource=Mock(return_value=source),
        LocationSet=SimpleNamespace(any=lambda: "all"),
    )
    monkeypatch.setitem(sys.modules, "guiderGDS", binding)
    return binding


async def fault_done(csc):
    """Wait for the fault scheduled by a processing thread."""
    for _ in range(20):
        if csc._processing_fault_task is not None:
            await asyncio.wait_for(csc._processing_fault_task, 2)
            return
        await asyncio.sleep(0)
    raise AssertionError("No worker fault task was scheduled")


async def test_simulation_never_imports_native_binding(csc, processor, monkeypatch):
    monkeypatch.setitem(sys.modules, "guiderGDS", None)
    await csc.start_guiding()
    try:
        assert csc.stamp_source is None
        assert csc._accept_stamps
        csc.process_stamp(
            np.zeros((10, 20)), SimpleNamespace(sensor_index=0, stamp_index=0)
        )
        processor.on_stamp.assert_called_once()
    finally:
        await csc.stop_guiding()


async def test_camera_is_ready_before_native_subscription(csc, processor, monkeypatch):
    events = []
    loop_thread = threading.get_ident()

    class Orientation:
        @property
        def camera(self):
            assert threading.get_ident() != loop_thread
            events.append("camera")
            return object()

    processor.combiner.orientation = Orientation()
    source = Mock()
    source.start_stamp_stream.side_effect = lambda callback: events.append("subscribe")
    binding = bind_source(monkeypatch, csc, source)
    await csc.start_guiding()
    try:
        assert events == ["camera", "subscribe"]
        binding.DaqStampSource.assert_called_once_with(
            partition="test", locations="all"
        )
        source.start_stamp_stream.assert_called_once_with(csc.process_stamp)
    finally:
        await csc.stop_guiding()
    source.stop_stamp_stream.assert_called_once()


async def test_previous_queue_callback_cannot_enter_new_run(csc, processor):
    await csc.start_guiding()
    old_queue = csc._publish_queue
    await csc.stop_guiding()
    await csc.start_guiding()
    csc.evt_test = SimpleNamespace(set_write=AsyncMock())
    csc._queue_payload(old_queue, "evt_test", {"value": 1})
    csc._enqueue("evt_test", {"value": 2})
    await csc.stop_guiding()
    assert old_queue.empty()
    csc.evt_test.set_write.assert_awaited_once_with(value=2)


async def test_defaults_overrides_and_reenable(csc, processor):
    await csc.start_guiding()
    first_queue = csc._publish_queue
    config = processor.factory.call_args.args[0]
    assert (config.seed_frames, config.min_snr) == (10, 10.0)
    await csc.start_guiding()
    processor.factory.assert_called_once()
    await asyncio.gather(csc.stop_guiding(), csc.stop_guiding())
    processor.finalize.assert_called_once()
    assert csc.processor is csc._publish_task is csc._publish_queue is None
    csc.config.seed_frames = 3
    csc.config.min_snr = 7
    next_processor = Mock()
    processor.factory.return_value = next_processor
    await csc.start_guiding()
    assert csc.processor is next_processor
    assert csc._publish_queue is not first_queue
    config = processor.factory.call_args.args[0]
    assert (config.seed_frames, config.min_snr) == (3, 7)
    await csc.stop_guiding()
    processor.finalize.assert_called_once()
    next_processor.finalize.assert_called_once()


async def test_worker_join_finalization_and_write_failure_order(csc, processor, caplog):
    await csc.start_guiding()
    events = []
    loop_thread = threading.get_ident()

    def stop():
        assert threading.get_ident() != loop_thread
        events.append("joined")
        csc._enqueue("evt_test", {"value": 2})

    def finalize():
        assert events == ["joined"]
        events.append("finalized")
        csc._enqueue("evt_test", {"value": 3})

    csc.stamp_source = SimpleNamespace(stop_stamp_stream=Mock(side_effect=stop))
    processor.finalize.side_effect = finalize
    error = RuntimeError("write failed")

    async def write(value):
        assert threading.get_ident() == loop_thread
        if value == 1:
            raise error

    csc.evt_test = SimpleNamespace(set_write=AsyncMock(side_effect=write))
    await asyncio.to_thread(csc._enqueue, "evt_test", {"value": 1})
    with caplog.at_level(logging.ERROR):
        await asyncio.gather(csc.stop_guiding(), csc.stop_guiding())
    assert events == ["joined", "finalized"]
    assert [
        call.kwargs["value"] for call in csc.evt_test.set_write.await_args_list
    ] == [1, 2, 3]
    assert "write failed" in caplog.text
    assert "sample is not retried" in caplog.text
    record = next(record for record in caplog.records if record.exc_info)
    assert record.exc_info[1] is error
    assert csc._run_error is None
    csc.fault.assert_not_awaited()


async def test_worker_processing_fault_is_terminal(csc, processor):
    await csc.start_guiding()
    error = RuntimeError("bad stamp")
    processor.on_stamp.side_effect = error
    metadata = SimpleNamespace(sensor_index=1, stamp_index=0, sequence=3)
    await asyncio.to_thread(csc.process_stamp, np.zeros((20, 20)), metadata)
    await fault_done(csc)
    csc.process_stamp(np.zeros((20, 20)), metadata)
    assert csc._run_error is error
    assert csc._run_error_code == ErrorCode.STAMP_PROCESSING
    assert csc.summary_state == salobj.State.FAULT
    processor.on_stamp.assert_called_once()
    processor.finalize.assert_not_called()
    assert csc.processor is csc._publish_task is csc._publish_queue is None
    assert csc.fault.await_count == 1


@pytest.mark.parametrize("prior_failure", [False, True])
async def test_failed_source_stop_keeps_handle_and_never_retries(
    csc, processor, prior_failure
):
    await csc.start_guiding()
    primary = RuntimeError("processing failed")
    secondary = RuntimeError("join failed")
    if prior_failure:
        csc._record_run_error(primary, ErrorCode.STAMP_PROCESSING)
    source = SimpleNamespace(stop_stamp_stream=Mock(side_effect=secondary))
    csc.stamp_source = source
    expected = primary if prior_failure else secondary
    for _ in range(2):
        with pytest.raises(RuntimeError) as caught:
            await csc.stop_guiding()
        assert caught.value is expected
    assert csc.stamp_source is source
    assert csc.processor is processor
    assert csc._publish_task is csc._publish_queue is None
    source.stop_stamp_stream.assert_called_once()
    processor.finalize.assert_not_called()
    with pytest.raises(salobj.ExpectedError) as blocked:
        await csc.start_guiding()
    assert "restart" in str(blocked.value.__cause__)
    source.stop_stamp_stream.assert_called_once()
    processor.factory.assert_called_once()
    if prior_failure:
        assert "join failed" in " ".join(primary.__notes__)


@pytest.mark.parametrize("prior_failure", [False, True])
@pytest.mark.parametrize("late_failure", [False, True])
async def test_source_stop_deadline_retains_pending_join(
    csc, processor, monkeypatch, caplog, prior_failure, late_failure
):
    monkeypatch.setattr(module, "SOURCE_STOP_TIMEOUT", 0.2)
    await csc.start_guiding()
    entered, release = threading.Event(), threading.Event()
    primary = RuntimeError("processing failed before timeout")
    late_error = RuntimeError("join failed after timeout")
    if prior_failure:
        csc._record_run_error(primary, ErrorCode.STAMP_PROCESSING)

    def stop():
        entered.set()
        assert release.wait(5)
        if late_failure:
            raise late_error

    source = SimpleNamespace(stop_stamp_stream=Mock(side_effect=stop))
    csc.stamp_source = source
    csc.evt_test = SimpleNamespace(set_write=AsyncMock())
    old_queue = csc._publish_queue
    csc._summary_state = salobj.State.DISABLED
    try:
        with pytest.raises(RuntimeError if prior_failure else TimeoutError) as caught:
            await asyncio.wait_for(csc.handle_summary_state(), 1)
        assert entered.is_set()
        future = csc._source_stop_future
        assert future is not None and not future.done()
        assert not future.cancelled()
        if prior_failure:
            assert caught.value is primary
            assert "native join is still pending" in " ".join(primary.__notes__)
            assert csc._run_error_code == ErrorCode.STAMP_PROCESSING
        else:
            assert "native join is still pending" in str(caught.value)
            assert csc._run_error_code == ErrorCode.SHUTDOWN
        assert csc.summary_state == salobj.State.FAULT
        assert csc.stamp_source is source
        assert csc.processor is processor
        assert csc._publish_task is csc._publish_queue is None
        assert csc._source_stop_failed
        assert not csc._accept_stamps and not csc._accept_publication
        processor.finalize.assert_not_called()
        await asyncio.to_thread(csc._enqueue, "evt_test", {"value": 1})
        csc._queue_payload(old_queue, "evt_test", {"value": 2})
        csc.evt_test.set_write.assert_not_awaited()
        assert old_queue.empty()
        with pytest.raises(type(caught.value)) as repeated:
            await csc.stop_guiding()
        assert repeated.value is caught.value
        with pytest.raises(salobj.ExpectedError, match="restart the CSC process"):
            await csc.start_guiding()
        source.stop_stamp_stream.assert_called_once()
    finally:
        release.set()
        if csc._source_stop_future is not None:
            await asyncio.gather(csc._source_stop_future, return_exceptions=True)
            await asyncio.sleep(0)
    if late_failure:
        assert "join failed after timeout" in " ".join(csc._run_error.__notes__)
        assert "join failed after timeout" in caplog.text
    else:
        assert "stop completed after its deadline" in caplog.text
    assert csc._source_stop_future.done()
    assert csc.stamp_source is source
    assert csc._source_stop_failed
    with pytest.raises(salobj.ExpectedError, match="restart the CSC process"):
        await csc.start_guiding()
    source.stop_stamp_stream.assert_called_once()
    processor.factory.assert_called_once()
    processor.finalize.assert_not_called()


async def test_cancelled_stop_caller_does_not_cancel_timed_out_join(
    csc, processor, monkeypatch
):
    monkeypatch.setattr(module, "SOURCE_STOP_TIMEOUT", 0.2)
    await csc.start_guiding()
    entered, release = threading.Event(), threading.Event()

    def stop():
        entered.set()
        assert release.wait(5)

    source = SimpleNamespace(stop_stamp_stream=Mock(side_effect=stop))
    csc.stamp_source = source
    caller = asyncio.create_task(csc.stop_guiding())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 1)
        assert isinstance(csc._run_error, TimeoutError)
        assert csc._source_stop_failed
        assert not csc._source_stop_future.done()
        assert not csc._source_stop_future.cancelled()
        assert csc._stop_guiding_task.done()
        assert csc.stamp_source is source
        assert csc._publish_task is None
        source.stop_stamp_stream.assert_called_once()
        processor.finalize.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(caller, csc._source_stop_future, return_exceptions=True)


async def test_finalization_failure_is_not_retried(csc, processor):
    await csc.start_guiding()
    error = RuntimeError("finalization failed")
    processor.finalize.side_effect = error
    for _ in range(2):
        with pytest.raises(RuntimeError) as caught:
            await csc.stop_guiding()
        assert caught.value is error
    processor.finalize.assert_called_once()
    assert csc.processor is csc._publish_task is csc._publish_queue is None
    assert csc._run_error_code == ErrorCode.STAMP_PROCESSING


async def test_reported_failure_allows_normal_state_recovery(csc, processor):
    await csc.start_guiding()
    error = RuntimeError("processing failed")
    csc._record_run_error(error, ErrorCode.STAMP_PROCESSING)
    csc._summary_state = salobj.State.DISABLED
    with pytest.raises(RuntimeError) as caught:
        await csc.handle_summary_state()
    assert caught.value is error
    assert csc.summary_state == salobj.State.FAULT
    csc._summary_state = salobj.State.STANDBY
    await csc.handle_summary_state()
    assert csc.summary_state == salobj.State.STANDBY
    csc._summary_state = salobj.State.ENABLED
    await csc.handle_summary_state()
    assert csc._run_error is None
    assert csc.processor is processor
    await csc.stop_guiding()


@pytest.mark.parametrize(
    "point", ["import", "camera", "construct", "start", "processor"]
)
async def test_startup_failure_cleans_resources_and_keeps_cause(
    csc, processor, monkeypatch, point
):
    source = Mock()
    binding = bind_source(monkeypatch, csc, source)
    error = RuntimeError(f"{point} failed")
    if point == "import":
        monkeypatch.setitem(sys.modules, "guiderGDS", None)
    elif point == "camera":

        class Orientation:
            @property
            def camera(self):
                raise error

        processor.combiner.orientation = Orientation()
    elif point == "construct":
        binding.DaqStampSource.side_effect = error
    elif point == "start":
        source.start_stamp_stream.side_effect = error
    else:
        processor.factory.side_effect = error
    with pytest.raises(salobj.ExpectedError) as caught:
        await csc.start_guiding()
    if point == "import":
        assert isinstance(caught.value.__cause__, ModuleNotFoundError)
    else:
        assert caught.value.__cause__ is error
        assert csc._run_error is error
    assert "guiderGDS binding" in str(caught.value)
    assert csc._run_error_code == ErrorCode.DAQ_SUBSCRIPTION
    assert csc.summary_state == salobj.State.FAULT
    assert csc.processor is csc.stamp_source is csc._publish_task is None
    processor.finalize.assert_not_called()
    assert source.stop_stamp_stream.call_count == (1 if point == "start" else 0)


async def test_start_and_stop_failure_preserve_primary(csc, processor, monkeypatch):
    primary, secondary = RuntimeError("start failed"), RuntimeError("stop failed")
    source = Mock()
    source.start_stamp_stream.side_effect = primary
    source.stop_stamp_stream.side_effect = secondary
    bind_source(monkeypatch, csc, source)
    with pytest.raises(salobj.ExpectedError) as caught:
        await csc.start_guiding()
    assert caught.value.__cause__ is primary
    assert "stop failed" in " ".join(primary.__notes__)
    assert csc.stamp_source is source
    assert csc._publish_task is None
    processor.finalize.assert_not_called()


async def test_publication_deadline_cancels_writer(csc, processor, monkeypatch, caplog):
    monkeypatch.setattr(module, "PUBLISH_DRAIN_TIMEOUT", 0.05)
    await csc.start_guiding()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def write(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    csc.evt_test = SimpleNamespace(set_write=AsyncMock(side_effect=write))
    csc._enqueue("evt_test", {"value": 1})
    csc._enqueue("evt_test", {"value": 2})
    await asyncio.wait_for(started.wait(), 2)
    with caplog.at_level(logging.ERROR), pytest.raises(TimeoutError):
        await csc.stop_guiding()
    assert cancelled.is_set()
    assert csc._publish_task is csc._publish_queue is None
    assert csc._run_error_code == ErrorCode.SHUTDOWN
    assert "1 queued payloads" in caplog.text
    csc.evt_test.set_write.assert_awaited_once()


async def test_cancel_resistant_writer_remains_owned(csc, processor, monkeypatch):
    monkeypatch.setattr(module, "PUBLISH_DRAIN_TIMEOUT", 0.05)
    await csc.start_guiding()
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(**kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    csc.evt_test = SimpleNamespace(set_write=AsyncMock(side_effect=write))
    csc._enqueue("evt_test", {"value": 1})
    csc._enqueue("evt_test", {"value": 2})
    task = csc._publish_task
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(TimeoutError):
            await csc.stop_guiding()
        assert csc._publish_task is task and not task.done()
        with pytest.raises(salobj.ExpectedError):
            await csc.start_guiding()
        assert csc._publish_task is task
    finally:
        release.set()
        if csc._run_error is None:
            await csc.stop_guiding()
        await asyncio.wait_for(task, 2)
    csc.evt_test.set_write.assert_awaited_once()


async def test_stop_cancellation_waits_for_native_join(csc, processor):
    await csc.start_guiding()
    entered, release = threading.Event(), threading.Event()

    def stop():
        entered.set()
        assert release.wait(2)

    csc.stamp_source = SimpleNamespace(stop_stamp_stream=Mock(side_effect=stop))
    caller = asyncio.create_task(csc.stop_guiding())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        caller.cancel()
        await asyncio.sleep(0)
        assert not csc._stop_guiding_task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 2)
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
    processor.finalize.assert_called_once()
    assert csc.stamp_source is csc.processor is csc._publish_task is None


async def test_start_cancellation_never_finalizes(csc, processor, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Orientation:
        @property
        def camera(self):
            entered.set()
            assert release.wait(2)

    processor.combiner.orientation = Orientation()
    bind_source(monkeypatch, csc, Mock())
    caller = asyncio.create_task(csc.start_guiding())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 2)
        assert csc.processor is csc._publish_task is None
        processor.finalize.assert_not_called()
        await csc.close_tasks()
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)


async def test_disable_racing_with_worker_error(csc, processor):
    await csc.start_guiding()
    entered, release = threading.Event(), threading.Event()
    error = RuntimeError("in-flight failure")

    def process(*args):
        entered.set()
        assert release.wait(2)
        raise error

    processor.on_stamp.side_effect = process
    metadata = SimpleNamespace(sensor_index=1, stamp_index=0)
    worker = threading.Thread(
        target=csc.process_stamp, args=(np.zeros((20, 20)), metadata)
    )
    joining = threading.Event()

    def join():
        joining.set()
        worker.join(timeout=5)
        assert not worker.is_alive()

    csc.stamp_source = SimpleNamespace(stop_stamp_stream=join)
    worker.start()
    stop = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        stop = asyncio.create_task(csc.stop_guiding())
        assert await asyncio.to_thread(joining.wait, 2)
        release.set()
        with pytest.raises(RuntimeError) as caught:
            await asyncio.wait_for(stop, 2)
        assert caught.value is error
    finally:
        release.set()
        await asyncio.to_thread(worker.join, 5)
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)
    await fault_done(csc)
    processor.finalize.assert_not_called()
    assert csc._publish_task is None


async def test_close_cancels_fault_task_and_calls_parent_on_failure(csc, processor):
    await csc.start_guiding()
    entered = asyncio.Event()

    async def pending_fault(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    csc.fault.side_effect = pending_fault
    processor.on_stamp.side_effect = RuntimeError("bad stamp")
    csc.process_stamp(
        np.zeros((20, 20)), SimpleNamespace(sensor_index=1, stamp_index=0)
    )
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(RuntimeError):
        await csc.close_tasks()
    assert csc._processing_fault_task.done()
    assert csc._publish_task is None
    salobj.ConfigurableCsc.close_tasks.assert_awaited_once()


async def test_failed_join_rejects_late_worker_publication(csc, processor):
    await csc.start_guiding()
    csc.evt_test = SimpleNamespace(set_write=AsyncMock())
    old_queue = csc._publish_queue
    csc.stamp_source = SimpleNamespace(
        stop_stamp_stream=Mock(side_effect=RuntimeError("stop failed"))
    )
    with pytest.raises(RuntimeError):
        await csc.stop_guiding()
    await asyncio.to_thread(csc._enqueue, "evt_test", {"value": 1})
    csc._queue_payload(old_queue, "evt_test", {"value": 2})
    csc.evt_test.set_write.assert_not_awaited()
    assert old_queue.empty()
