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
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from lsst.ts.guider.demo import run_daq_guider_pipeline_demo
from lsst.ts.guider.pipeline import GuiderTrackerConfig, StreamingGuiderProcessor

demo = importlib.import_module("lsst.ts.guider.demo.run_daq_guider_pipeline_demo")


@pytest.fixture
def source(monkeypatch):
    source = Mock()
    binding = SimpleNamespace(
        DaqStampSource=Mock(return_value=source),
        LocationSet=SimpleNamespace(any=Mock(return_value="locations")),
    )
    monkeypatch.setitem(sys.modules, "guiderGDS", binding)
    return source


def test_help_does_not_require_binding(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "guiderGDS", None)
    with pytest.raises(SystemExit) as exc:
        run_daq_guider_pipeline_demo(["--help"])
    assert exc.value.code == 0
    assert "--per-frame" in capsys.readouterr().out


def test_stamp_limit_ignores_callbacks_after_limit_and_stops_source(source):
    processor = Mock(metrics=SimpleNamespace(total_stamps=0))

    def count(pixels, metadata):
        processor.metrics.total_stamps += 1

    processor.on_stamp.side_effect = count
    source.start_stamp_stream.side_effect = lambda callback: [
        callback(None, None) for _ in range(5)
    ]
    demo.run_live(processor, "test", 2, 1)
    assert processor.on_stamp.call_count == 2
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
    done.wait.side_effect = KeyboardInterrupt
    monkeypatch.setattr(demo.threading, "Event", Mock(return_value=done))
    demo.run_live(processor, "test", 0, 0)
    source.stop_stamp_stream.assert_called_once_with()


def test_main_finalizes_and_reports_even_on_failure(monkeypatch):
    processor = Mock(combined_offsets=[])
    monkeypatch.setattr(demo, "StreamingGuiderProcessor", Mock(return_value=processor))
    monkeypatch.setattr(demo, "run_live", Mock(side_effect=ValueError("failed")))
    report = Mock()
    monkeypatch.setattr(demo, "report", report)
    with pytest.raises(ValueError, match="failed"):
        run_daq_guider_pipeline_demo([])
    processor.finalize.assert_called_once_with()
    report.assert_called_once_with(processor)


def test_empty_report_and_exit_code(source, monkeypatch, capsys):
    monkeypatch.setattr(demo, "run_live", Mock())
    assert run_daq_guider_pipeline_demo([]) == 1
    assert "no acquisition had a valid sensor" in capsys.readouterr().out


def test_live_logger_reports_combine_cost_not_callback_latency(caplog):
    from lsst.ts.guider.pipeline import CombinedOffset

    processor = StreamingGuiderProcessor(GuiderTrackerConfig())
    processor.metrics.combine_times.append(0.001)
    processor.metrics.callback_times.append(0.100)
    result = CombinedOffset(42, 2, -1, 2, 2, 0.2, 0.3, 0.1, 0.15)
    with caplog.at_level("INFO", logger="guider_pipeline"):
        demo.make_combined_offset_logger(processor)(result)
    assert "combine 1.000 ms" in caplog.text
    assert "100.000 ms" not in caplog.text


def test_metrics_duration_and_empty_timing_summary():
    from lsst.ts.guider.demo.streaming_report import summarize_milliseconds
    from lsst.ts.guider.pipeline import StreamingMetrics

    metrics = StreamingMetrics()
    assert metrics.stream_seconds == 0
    metrics.first_stamp_time = 10
    metrics.last_stamp_time = 12
    assert metrics.stream_seconds == 2
    assert summarize_milliseconds([]) == "n=0"
    assert "mean=2.000" in summarize_milliseconds([0.001, 0.003])
