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

import importlib
import logging
import math
import sys
import warnings
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from lsst.ts.guider.demo.run_daq_guider_pipeline_demo import (
    run_daq_guider_pipeline_demo,
)
from lsst.ts.guider.pipeline import (
    CombinedOffset,
    GuiderTrackerConfig,
    StreamingGuiderProcessor,
    StreamingMetrics,
)

demo = importlib.import_module("lsst.ts.guider.demo.run_daq_guider_pipeline_demo")
reporting = importlib.import_module("lsst.ts.guider.demo.streaming_report")


@pytest.fixture(autouse=True)
def restore_logging():
    logger = logging.getLogger("lsst.ts.guider")
    original = logger.level
    yield
    logger.setLevel(original)


@pytest.fixture
def source(monkeypatch):
    """Supply a controllable DAQ source without connecting to a partition."""
    source = Mock()
    binding = SimpleNamespace(
        DaqStampSource=Mock(return_value=source),
        LocationSet=SimpleNamespace(any=Mock(return_value="locations")),
    )
    monkeypatch.setitem(sys.modules, "guiderGDS", binding)
    return source


def test_import_and_help_do_not_require_binding(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "guiderGDS", None)
    module = importlib.reload(demo)
    with pytest.raises(SystemExit) as exc:
        module.run_daq_guider_pipeline_demo(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--per-frame" in output
    assert "--log-level" in output


def test_stamp_limit_ignores_callbacks_after_limit_and_stops_source(source):
    processor = Mock(metrics=SimpleNamespace(total_stamps=0))
    arrivals = [(object(), object()) for _ in range(5)]

    def count(pixels, metadata):
        processor.metrics.total_stamps += 1

    processor.on_stamp.side_effect = count

    def deliver(callback):
        for pixels, metadata in arrivals:
            callback(pixels, metadata)

    source.start_stamp_stream.side_effect = deliver
    demo.run_live(processor, "test", 2, 1)
    assert processor.on_stamp.call_args_list == [call(*args) for args in arrivals[:2]]
    sys.modules["guiderGDS"].DaqStampSource.assert_called_once_with(
        partition="test", locations="locations"
    )
    source.stop_stamp_stream.assert_called_once_with()


def test_callback_exception_is_raised_after_source_stops(source):
    processor = Mock(metrics=SimpleNamespace(total_stamps=0))
    processor.on_stamp.side_effect = ValueError("bad stamp")
    source.start_stamp_stream.side_effect = lambda callback: callback(None, None)
    with pytest.raises(ValueError, match="bad stamp"):
        demo.run_live(processor, "test", 2, 1)
    source.stop_stamp_stream.assert_called_once_with()


def test_timeout_stops_source(source, monkeypatch):
    processor = Mock(metrics=SimpleNamespace(total_stamps=0))
    monkeypatch.setattr(demo, "perf_counter", Mock(side_effect=[0, 2]))
    demo.run_live(processor, "test", 0, 1)
    source.stop_stamp_stream.assert_called_once_with()


def test_interrupt_stops_source(source, monkeypatch):
    processor = Mock(metrics=SimpleNamespace(total_stamps=0))
    done = Mock()
    done.is_set.return_value = False
    done.wait.side_effect = KeyboardInterrupt
    monkeypatch.setattr(demo.threading, "Event", Mock(return_value=done))
    demo.run_live(processor, "test", 0, 0)
    source.stop_stamp_stream.assert_called_once_with()


def test_main_preserves_run_failure_without_finalizing_or_reporting(monkeypatch):
    processor = Mock(combined_offsets=[])
    monkeypatch.setattr(demo, "StreamingGuiderProcessor", Mock(return_value=processor))
    monkeypatch.setattr(demo, "run_live", Mock(side_effect=ValueError("failed")))
    report = Mock()
    monkeypatch.setattr(demo, "report", report)
    with pytest.raises(ValueError, match="failed"):
        run_daq_guider_pipeline_demo([])
    processor.finalize.assert_not_called()
    report.assert_not_called()


def test_empty_report_and_exit_code(monkeypatch, caplog, capsys):
    monkeypatch.setattr(demo, "run_live", Mock())
    with caplog.at_level(logging.INFO):
        assert run_daq_guider_pipeline_demo([]) == 1
    assert "no acquisition had a valid sensor" in caplog.text
    assert capsys.readouterr().out == ""


def test_combined_but_rejected_results_still_return_success(monkeypatch, caplog):
    processor = StreamingGuiderProcessor(GuiderTrackerConfig())
    processor.combined_offsets.append(
        CombinedOffset(
            stamp_index=0,
            combined_dx=math.nan,
            combined_dy=math.nan,
            n_valid=0,
            n_total=2,
            scatter_dx=math.nan,
            scatter_dy=math.nan,
        )
    )
    monkeypatch.setattr(demo, "StreamingGuiderProcessor", Mock(return_value=processor))
    monkeypatch.setattr(demo, "run_live", Mock())
    with caplog.at_level(logging.INFO):
        assert run_daq_guider_pipeline_demo([]) == 0
    assert "no acquisition had a valid sensor" in caplog.text


def test_live_logger_reports_combine_cost_not_callback_latency(caplog):
    processor = StreamingGuiderProcessor(GuiderTrackerConfig())
    processor.metrics.combine_times.append(0.001)
    processor.metrics.callback_times.append(0.100)
    result = CombinedOffset(
        stamp_index=42,
        combined_dx=2,
        combined_dy=-1,
        n_valid=2,
        n_total=2,
        scatter_dx=0.2,
        scatter_dy=0.3,
        error_dx=0.1,
        error_dy=0.15,
    )
    with caplog.at_level("INFO", logger=demo.__name__):
        demo.make_combined_offset_logger(processor)(result)
    assert "combine 1.000 ms" in caplog.text
    assert "100.000 ms" not in caplog.text


def test_metrics_duration_and_empty_timing_summary():
    metrics = StreamingMetrics()
    assert metrics.stream_seconds == 0
    metrics.first_stamp_time = 10
    metrics.last_stamp_time = 12
    assert metrics.stream_seconds == 2
    assert reporting.summarize_milliseconds([]) == "n=0"
    assert reporting.summarize_milliseconds([0.001, 0.003]) == (
        "n=2 mean=2.000 median=2.000 p95=2.900 max=3.000 ms"
    )


def test_missing_binding_explains_environment_and_preserves_cause(monkeypatch):
    monkeypatch.setitem(sys.modules, "guiderGDS", None)
    with pytest.raises(RuntimeError, match="CCSDAQ.*PYTHONPATH") as caught:
        demo.run_live(Mock(), "test", 1, 1)
    assert isinstance(caught.value.__cause__, ImportError)
    assert "LD_LIBRARY_PATH" in str(caught.value)


@pytest.mark.parametrize(
    "args",
    [
        ["--max-stamps=-1"],
        ["--timeout=-1"],
        ["--timeout=nan"],
        ["--timeout=inf"],
        ["--timeout=-inf"],
        ["--seed-frames=0"],
        ["--seed-frames=-1"],
        ["--min-snr=-1"],
        ["--min-snr=nan"],
        ["--min-snr=inf"],
        ["--min-snr=-inf"],
        ["--progress=-1"],
        ["--log-level=verbose"],
    ],
)
def test_cli_rejects_invalid_arguments_before_creating_processor(monkeypatch, args):
    factory = Mock()
    monkeypatch.setattr(demo, "StreamingGuiderProcessor", factory)
    with pytest.raises(SystemExit) as caught:
        run_daq_guider_pipeline_demo(args)
    assert caught.value.code == 2
    factory.assert_not_called()


def test_cli_accepts_zero_limits_and_uses_config_defaults(monkeypatch):
    processor = Mock(combined_offsets=[])
    factory = Mock(return_value=processor)
    runner = Mock()
    monkeypatch.setattr(demo, "StreamingGuiderProcessor", factory)
    monkeypatch.setattr(demo, "run_live", runner)
    monkeypatch.setattr(demo, "report", Mock())
    assert (
        run_daq_guider_pipeline_demo(
            ["--max-stamps=0", "--timeout=0", "--min-snr=0", "--progress=0"]
        )
        == 1
    )
    runner.assert_called_once_with(processor, "gds-emu", 0, 0.0)
    config = factory.call_args.args[0]
    assert config.seed_frames == GuiderTrackerConfig.seed_frames
    assert config.min_snr == 0
    processor.finalize.assert_called_once_with()


@pytest.mark.parametrize("per_frame", [False, True])
def test_cli_forwards_processing_options_and_selects_callbacks(monkeypatch, per_frame):
    processor = Mock(combined_offsets=[], on_combined_offset=None)
    factory = Mock(return_value=processor)
    runner = Mock()
    monkeypatch.setattr(demo, "StreamingGuiderProcessor", factory)
    monkeypatch.setattr(demo, "run_live", runner)
    monkeypatch.setattr(demo, "report", Mock())
    args = [
        "--partition=test",
        "--max-stamps=3",
        "--timeout=1.5",
        "--seed-frames=4",
        "--min-snr=7.5",
        "--progress=11",
    ]
    if per_frame:
        args.append("--per-frame")
    assert run_daq_guider_pipeline_demo(args) == 1
    config = factory.call_args.args[0]
    assert (config.seed_frames, config.min_snr) == (4, 7.5)
    factory.assert_called_once_with(
        config,
        progress_interval=11,
        on_visit_start=demo.log_visit_start if per_frame else None,
        on_visit_complete=demo.log_visit_summary,
    )
    runner.assert_called_once_with(processor, "test", 3, 1.5)
    if per_frame:
        assert callable(processor.on_combined_offset)
    else:
        assert processor.on_combined_offset is None


def test_short_timeout_bounds_the_wait(source, monkeypatch):
    processor = Mock(metrics=SimpleNamespace(total_stamps=0))
    done = Mock()
    done.is_set.return_value = False
    monkeypatch.setattr(demo.threading, "Event", Mock(return_value=done))
    monkeypatch.setattr(demo, "perf_counter", Mock(side_effect=[0, 0.025, 0.1]))
    demo.run_live(processor, "test", 0, 0.1)
    assert done.wait.call_args.args[0] == pytest.approx(0.075)
    assert done.wait.call_count == 1
    source.stop_stamp_stream.assert_called_once_with()


@pytest.mark.parametrize("failure", ["callback", "start", "wait"])
@pytest.mark.parametrize("stop_fails", [False, True])
def test_run_failure_survives_shutdown(source, monkeypatch, failure, stop_fails):
    processor = Mock(metrics=SimpleNamespace(total_stamps=0))
    original = ValueError("primary failure")
    secondary = RuntimeError("shutdown failed")
    if failure == "callback":
        processor.on_stamp.side_effect = original
        source.start_stamp_stream.side_effect = lambda callback: callback(None, None)
    elif failure == "start":
        source.start_stamp_stream.side_effect = original
    else:
        done = Mock()
        done.is_set.return_value = False
        done.wait.side_effect = original
        monkeypatch.setattr(demo.threading, "Event", Mock(return_value=done))
    if stop_fails:
        source.stop_stamp_stream.side_effect = secondary
    with pytest.raises(ValueError) as caught:
        demo.run_live(processor, "test", 0, 0)
    assert caught.value is original
    source.stop_stamp_stream.assert_called_once_with()
    processor.finalize.assert_not_called()
    if stop_fails:
        assert original.__cause__ is secondary
        assert "shutdown failed" in original.__notes__[0]


def test_shutdown_failure_is_not_silenced(source):
    processor = Mock(metrics=SimpleNamespace(total_stamps=0))
    processor.on_stamp.side_effect = lambda *args: setattr(
        processor.metrics, "total_stamps", 1
    )
    source.start_stamp_stream.side_effect = lambda callback: callback(None, None)
    error = RuntimeError("cannot join worker")
    source.stop_stamp_stream.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        demo.run_live(processor, "test", 1, 1)
    assert caught.value is error


@pytest.mark.parametrize(
    "failure", [None, "camera", "callback", "stop", "finalize", "report"]
)
def test_main_orders_camera_stream_shutdown_finalization_and_report(
    source, monkeypatch, failure
):
    events = []
    error = ValueError(failure)
    processor = Mock(
        combined_offsets=[object()], metrics=SimpleNamespace(total_stamps=0)
    )

    class Orientation:
        @property
        def camera(self):
            events.append("camera")
            if failure == "camera":
                raise error
            return object()

    processor.combiner.orientation = Orientation()

    def process(*args):
        events.append("stamp")
        if failure == "callback":
            raise error
        processor.metrics.total_stamps += 1

    def start(callback):
        events.append("start")
        callback(None, None)

    def stop():
        events.append("stop")
        if failure == "stop":
            raise error

    def finalize():
        events.append("finalize")
        if failure == "finalize":
            raise error

    def report(processor):
        events.append("report")
        if failure == "report":
            raise error

    processor.on_stamp.side_effect = process
    processor.finalize.side_effect = finalize
    source.start_stamp_stream.side_effect = start
    source.stop_stamp_stream.side_effect = stop
    monkeypatch.setattr(demo, "StreamingGuiderProcessor", Mock(return_value=processor))
    monkeypatch.setattr(demo, "report", report)
    if failure is None:
        assert run_daq_guider_pipeline_demo(["--max-stamps=1"]) == 0
    else:
        with pytest.raises(ValueError) as caught:
            run_daq_guider_pipeline_demo(["--max-stamps=1"])
        assert caught.value is error
    expected = ["camera"]
    if failure != "camera":
        expected.extend(["start", "stamp", "stop"])
    if failure not in ("camera", "callback", "stop"):
        expected.append("finalize")
    if failure in (None, "report"):
        expected.append("report")
    assert events == expected


@pytest.mark.parametrize("level", ["INFO", "WARNING", "ERROR", "CRITICAL", "debug"])
def test_cli_level_filters_all_reports(monkeypatch, caplog, capsys, level):
    result = CombinedOffset(
        stamp_index=42,
        combined_dx=2,
        combined_dy=-1,
        n_valid=2,
        n_total=2,
        scatter_dx=0.2,
        scatter_dy=0.3,
        error_dx=0.1,
        error_dy=0.15,
    )
    processor = StreamingGuiderProcessor(GuiderTrackerConfig())
    processor.combined_offsets.append(result)
    processor.metrics.combine_times.append(0.001)
    monkeypatch.setattr(demo, "StreamingGuiderProcessor", Mock(return_value=processor))
    monkeypatch.setattr(processor, "finalize", Mock())

    def run(*args):
        demo.log_visit_start(7, "image")
        demo.log_visit_summary(7, [result])
        processor.on_combined_offset(result)

    monkeypatch.setattr(demo, "run_live", run)
    with caplog.at_level(logging.DEBUG):
        assert run_daq_guider_pipeline_demo(["--per-frame", "--log-level", level]) == 0
    if level.upper() in ("INFO", "DEBUG"):
        assert "visit start" in caplog.text
        assert "visit (sequence 7) complete" in caplog.text
        assert "combine 1.000 ms" in caplog.text
        assert "DAQ guider pipeline metrics" in caplog.text
    else:
        assert not [
            record
            for record in caplog.records
            if record.name.startswith("lsst.ts.guider")
        ]
    assert capsys.readouterr().out == ""


def test_single_sensor_nan_errors_are_reported_without_warnings(caplog):
    p = StreamingGuiderProcessor(GuiderTrackerConfig())
    p.combined_offsets.append(
        CombinedOffset(
            stamp_index=0,
            combined_dx=1,
            combined_dy=2,
            n_valid=1,
            n_total=1,
            scatter_dx=math.nan,
            scatter_dy=math.nan,
        )
    )
    with warnings.catch_warnings(), caplog.at_level(
        logging.INFO, logger="lsst.ts.guider"
    ):
        warnings.simplefilter("error", RuntimeWarning)
        reporting.report(p)
        reporting.log_visit_summary(7, p.combined_offsets)
    assert "nan, nan" in caplog.text
    assert "combine frame        : camera" in caplog.text
    assert "overall dx" not in caplog.text


def test_report_separates_temporal_medians_and_acquisition_errors(caplog):
    p = StreamingGuiderProcessor(GuiderTrackerConfig())
    p.combined_offsets.extend(
        [
            CombinedOffset(
                stamp_index=0,
                combined_dx=1,
                combined_dy=-2,
                n_valid=2,
                n_total=2,
                scatter_dx=0.4,
                scatter_dy=0.6,
                error_dx=0.1,
                error_dy=0.2,
            ),
            CombinedOffset(
                stamp_index=1,
                combined_dx=3,
                combined_dy=-4,
                n_valid=2,
                n_total=2,
                scatter_dx=0.8,
                scatter_dy=1.0,
                error_dx=0.3,
                error_dy=0.4,
            ),
            CombinedOffset(
                stamp_index=2,
                combined_dx=math.nan,
                combined_dy=math.nan,
                n_valid=0,
                n_total=2,
                scatter_dx=math.nan,
                scatter_dy=math.nan,
            ),
            CombinedOffset(
                stamp_index=3,
                combined_dx=5,
                combined_dy=-6,
                n_valid=1,
                n_total=1,
                scatter_dx=math.nan,
                scatter_dy=math.nan,
            ),
        ]
    )
    with caplog.at_level(logging.INFO, logger="lsst.ts.guider"):
        reporting.report(p)
        reporting.log_visit_summary(7, p.combined_offsets)
    assert "3/4 with >=1 valid sensor" in caplog.text
    assert "median offsets dx, dy: +3.000, -4.000" in caplog.text
    assert "temporal std dx, dy  : 1.633, 1.633" in caplog.text
    assert "median acquisition std errors: 0.200, 0.300" in caplog.text
    assert "median offsets dx, dy         : +3.000, -4.000 px" in caplog.text
    assert "median acquisition std errors : 0.200, 0.300 px" in caplog.text


@pytest.mark.parametrize("empty", [False, True])
def test_series_report_with_no_accepted_results(caplog, empty):
    offsets = (
        []
        if empty
        else [
            CombinedOffset(
                stamp_index=0,
                combined_dx=math.nan,
                combined_dy=math.nan,
                n_valid=0,
                n_total=2,
                scatter_dx=math.nan,
                scatter_dy=math.nan,
            )
        ]
    )
    with caplog.at_level(logging.INFO, logger="lsst.ts.guider"):
        reporting.log_visit_summary(7, offsets)
    assert "no valid offsets" in caplog.text
