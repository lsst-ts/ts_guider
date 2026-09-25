# This file is part of ts_guider.
#
# Developed for Vera C. Rubin Observatory Telescope and Site Systems.
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

from types import SimpleNamespace

import numpy as np
import pytest
from lsst.ts.guider.pipeline import (
    CentroidMeasurement,
    GuiderTrackerConfig,
    StreamingGuiderProcessor,
)
from lsst.ts.guider.pipeline import streaming


@pytest.fixture
def fake_tracker(monkeypatch):
    """Control lock timing while using the real stream and offset combiner."""

    class FakeTracker:
        lock_after = {}

        def __init__(self, sensor_name, config):
            self.sensor_name = sensor_name
            self.config = config
            self.reference_center = None
            self.attempts = []
            self.measured = []

        def lock_reference(self, stamps):
            self.attempts.append(stamps.copy())
            threshold = self.lock_after.get(self.sensor_name, self.config.seed_frames)
            if len(stamps) < threshold:
                return False
            self.reference_center = (0, 0)
            return True

        def measure(self, stamp):
            self.measured.append(stamp.copy())
            value = float(stamp[0, 0])
            return CentroidMeasurement(
                x=value, y=0, snr=100, converged=True, passed_quality=value >= 0
            )

    monkeypatch.setattr(streaming, "SensorTracker", FakeTracker)
    return FakeTracker


def send_stamp(processor, index, value=None, sensor=0, sequence=1):
    """Send one small, identifiable image through the public callback."""
    if value is None:
        value = index
    processor.on_stamp(
        np.full((2, 2), value, dtype=np.float32),
        SimpleNamespace(sensor_index=sensor, stamp_index=index, sequence=sequence),
    )


def test_failed_prefix_is_retained_then_replayed_once(fake_tracker):
    fake_tracker.lock_after["a"] = 12
    emitted = []
    processor = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=10),
        sensor_names={0: "a"},
        on_combined_offset=emitted.append,
    )
    values = [-1] + list(range(1, 12))
    for index, value in enumerate(values):
        send_stamp(processor, index + 50, value=value)
        if index in (9, 10):
            assert len(processor.seed_buffers["a"]) == index + 1
            assert processor.seed_indices["a"] == list(range(50, 51 + index))
            assert processor.acquisitions == {}
    tracker = processor.trackers["a"]
    assert [len(stamps) for stamps in tracker.attempts] == [10, 11, 12]
    for attempt in tracker.attempts:
        assert list(attempt[:, 0, 0]) == values[: len(attempt)]
    assert [stamp[0, 0] for stamp in tracker.measured] == values
    assert processor.seed_buffers["a"] == []
    assert processor.seed_indices["a"] == []
    assert sorted(processor.acquisitions) == list(range(50, 62))
    assert not processor.acquisitions[50]["a"].passed_quality
    assert [processor.acquisitions[i + 50]["a"].x for i in range(12)] == values
    assert len(processor.metrics.lock_times) == 3
    assert len(processor.metrics.measure_times) == 12
    assert processor.metrics.seed_stamps == 12
    assert processor.metrics.valid_measurements == 11
    assert processor.metrics.invalid_measurements == 1
    assert emitted == []

    send_stamp(processor, 62)
    send_stamp(processor, 63)
    assert [len(stamps) for stamps in tracker.attempts] == [10, 11, 12]
    assert len(tracker.measured) == 14
    assert [result.stamp_index for result in emitted] == [62]
    processor.finalize()
    assert [result.stamp_index for result in processor.combined_offsets] == list(
        range(50, 64)
    )
    assert [result.stamp_index for result in emitted] == [62]


@pytest.mark.parametrize("dtype", [np.float32, np.int32])
def test_reused_input_does_not_overwrite_failed_seeds(fake_tracker, dtype):
    fake_tracker.lock_after["a"] = 3
    processor = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=2), sensor_names={0: "a"}
    )
    pixels = np.empty((2, 2), dtype=dtype)
    for index, value in enumerate([10, 20, 30]):
        pixels[:] = value
        processor.on_stamp(
            pixels, SimpleNamespace(sensor_index=0, stamp_index=index, sequence=1)
        )
        if index < 2:
            assert not np.shares_memory(processor.seed_buffers["a"][-1], pixels)
    assert [stamp[0, 0] for stamp in processor.trackers["a"].measured] == [10, 20, 30]


def test_late_sensor_replay_fills_open_indices_without_reopening_closed(fake_tracker):
    fake_tracker.lock_after.update(a=2, b=5)
    emitted = []
    processor = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=2),
        sensor_names={0: "a", 1: "b"},
        on_combined_offset=emitted.append,
    )
    for index in range(7):
        send_stamp(processor, index, sensor=0)
        send_stamp(processor, index, sensor=1)
        if index == 4:
            assert [result.stamp_index for result in emitted] == [2, 3]
            assert set(processor.acquisitions) == {0, 1, 4}
            assert all(
                set(row) == {"a", "b"} for row in processor.acquisitions.values()
            )
        assert processor._combined_indices.isdisjoint(processor.acquisitions)
    assert [result.stamp_index for result in emitted] == [2, 3, 4, 5]
    assert [result.n_valid for result in emitted] == [1, 1, 2, 2]
    assert len(processor.trackers["b"].measured) == 7
    assert processor.metrics.valid_measurements == 14
    processor.finalize()
    assert processor.acquisitions == {}
    assert [result.stamp_index for result in processor.combined_offsets] == list(
        range(7)
    )
    assert [result.n_valid for result in processor.combined_offsets] == [
        2,
        2,
        1,
        1,
        2,
        2,
        2,
    ]
    assert [result.stamp_index for result in emitted] == [2, 3, 4, 5]


@pytest.mark.parametrize("seed_frames", [1, 3])
def test_perpetual_failure_retries_each_new_stamp(fake_tracker, seed_frames):
    fake_tracker.lock_after["a"] = 100
    processor = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=seed_frames), sensor_names={0: "a"}
    )
    for index in range(8):
        send_stamp(processor, index)
    tracker = processor.trackers["a"]
    assert [len(stamps) for stamps in tracker.attempts] == list(range(seed_frames, 9))
    assert len(processor.seed_buffers["a"]) == 8
    assert processor.seed_indices["a"] == list(range(8))
    assert tracker.measured == []
    assert processor.combined_offsets == []
    processor.finalize()
    assert [len(stamps) for stamps in tracker.attempts] == list(range(seed_frames, 9))


def test_short_series_does_not_lock_at_finalization(fake_tracker):
    processor = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=5), sensor_names={0: "a"}
    )
    for index in range(4):
        send_stamp(processor, index)
    processor.finalize()
    assert processor.trackers["a"].attempts == []
    assert processor.combined_offsets == []


def test_new_sequence_discards_failed_prefix(fake_tracker):
    fake_tracker.lock_after["a"] = 6
    processor = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=3), sensor_names={0: "a"}
    )
    for index in range(5):
        send_stamp(processor, index, value=100 + index)
    old_tracker = processor.trackers["a"]
    assert [len(stamps) for stamps in old_tracker.attempts] == [3, 4, 5]
    for index in range(6):
        send_stamp(processor, index, sequence=2)
        if index == 0:
            assert processor.seed_indices["a"] == [0]
            assert len(processor.seed_buffers["a"]) == 1
    tracker = processor.trackers["a"]
    assert tracker is not old_tracker
    assert [len(stamps) for stamps in tracker.attempts] == [3, 4, 5, 6]
    assert [stamp[0, 0] for stamp in tracker.measured] == list(range(6))
