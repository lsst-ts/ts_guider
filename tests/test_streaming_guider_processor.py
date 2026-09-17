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
from types import SimpleNamespace

import numpy as np
import pytest
from lsst.ts.guider.pipeline import (
    CentroidMeasurement,
    GuiderTrackerConfig,
    MultiSensorRunner,
    StreamingGuiderProcessor,
)


@pytest.fixture
def stream(monkeypatch):
    """Use known centroids to isolate stream ordering from image fitting."""
    module = importlib.import_module(
        "lsst.ts.guider.pipeline.streaming_guider_processor"
    )

    class Tracker:
        def __init__(self, name, config):
            self.reference_center = None

        def lock_reference(self, stamps):
            if np.all(stamps[:, 0, 0] < 0):
                return False
            self.reference_center = tuple(stamps[0, 0])
            return True

        def measure(self, stamp):
            return CentroidMeasurement(
                x=float(stamp[0, 0]),
                y=float(stamp[0, 1]),
                snr=100,
                fwhm=4,
                e1=0,
                e2=0,
                converged=True,
                passed_quality=bool(stamp[1, 0] >= 0),
            )

    monkeypatch.setattr(module, "SensorTracker", Tracker)
    live, starts, visits = [], [], []
    processor = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=2),
        sensor_names={0: "A", 1: "B"},
        on_combined_offset=live.append,
        on_visit_start=lambda sequence, label: starts.append((sequence, label)),
        on_visit_complete=lambda sequence, offsets: visits.append((sequence, offsets)),
    )
    return SimpleNamespace(processor=processor, live=live, starts=starts, visits=visits)


def feed(processor, sensor, index, xy=(0, 0), sequence=7, **metadata):
    stamp = np.array([xy, (1, 1)], dtype=np.float32)
    processor.on_stamp(
        stamp,
        SimpleNamespace(
            sensor_index=sensor,
            stamp_index=index,
            sequence=sequence,
            segment=None,
            startrow=0,
            startcol=0,
            obs_id="image",
            series_id="roi",
            **metadata,
        ),
    )


def test_warmup_live_results_and_idempotent_finalization(stream):
    p = stream.processor
    for index in range(5):
        for sensor in (0, 1):
            feed(p, sensor, index, (max(0, index - 1), 0))
    assert [offset.stamp_index for offset in stream.live] == [2, 3]
    p.finalize()
    p.finalize()
    assert stream.starts == [(7, "image")]
    assert len(stream.visits) == 1
    assert [offset.stamp_index for offset in p.combined_offsets] == list(range(5))
    assert [offset.combined_dx for offset in p.combined_offsets] == [0, 0, 1, 2, 3]
    assert all(offset.n_total == 2 for offset in p.combined_offsets)
    assert p.acquisitions == {}
    assert p.metrics.total_stamps == 10
    assert p.metrics.seed_stamps == 4
    assert len(p.metrics.measure_times) == 10
    assert len(p.metrics.callback_times) == 10
    assert len(p.metrics.combine_times) == 5
    # Cleanup results belong to the visit summary, not the live callback.
    assert [offset.stamp_index for offset in stream.live] == [2, 3]


def test_missing_sensor_is_omitted_from_combined_result(stream):
    p = stream.processor
    for index in range(3):
        for sensor in (0, 1):
            feed(p, sensor, index)
    feed(p, 0, 3, (3, 0))
    feed(p, 0, 4)
    result = next(offset for offset in stream.live if offset.stamp_index == 3)
    assert result.n_total == result.n_valid == 1
    assert 3 not in p.acquisitions
    p.finalize()
    assert result.combined_dx == 3
    assert sum(offset.stamp_index == 3 for offset in p.combined_offsets) == 1


def test_staggered_lock_replays_only_acquisitions_still_pending(stream):
    p = stream.processor
    feed(p, 0, 0)
    feed(p, 0, 1)
    feed(p, 0, 2)
    feed(p, 1, 2)
    feed(p, 0, 3)  # Close acquisition 2 while B is still seeding.
    feed(p, 1, 3)  # B locks, but must not recreate acquisition 2.
    assert p.metrics.discarded_seed_measurements == 1
    assert 2 not in p.acquisitions
    p.finalize()
    assert [offset.n_total for offset in p.combined_offsets] == [1, 1, 1, 2]
    assert p.acquisitions == {}


def test_failed_lock_retries_with_next_full_seed_window(stream):
    p = stream.processor
    feed(p, 0, 0, (-1, 0))
    feed(p, 0, 1, (-1, 0))
    assert not p.references
    feed(p, 0, 2, (10, 20))
    feed(p, 0, 3, (10, 20))
    feed(p, 0, 4, (11, 18))
    p.finalize()
    assert len(p.metrics.lock_times) == 2
    assert [offset.stamp_index for offset in p.combined_offsets] == [2, 3, 4]
    assert (p.combined_offsets[-1].combined_dx, p.combined_offsets[-1].combined_dy) == (
        1,
        -2,
    )


def test_empty_and_short_series_finalize_once_without_locking(stream):
    p = stream.processor
    p.finalize()
    assert stream.visits == []
    feed(p, 0, 0)
    p.finalize()
    p.finalize()
    assert stream.visits == [(7, [])]
    assert not p.references
    assert not p.seed_buffers
    feed(p, 0, 1)
    assert not p.references


@pytest.mark.parametrize("old,new", [(7, 8), (65535, 0)])
def test_series_reset(stream, old, new):
    p = stream.processor
    for index in range(3):
        feed(p, 0, index, (0, 0), sequence=old)
    feed(p, 0, 0, (100, 200), sequence=new)
    assert set(p.trackers) == {"A"}
    feed(p, 0, 1, (100, 200), sequence=new)
    feed(p, 0, 2, (102, 199), sequence=new)
    p.finalize()
    p.finalize()
    assert [sequence for sequence, _ in stream.visits] == [old, new]
    assert [len(offsets) for _, offsets in stream.visits] == [3, 3]
    assert p.references == {"A": (100, 200)}
    last = stream.visits[-1][1][-1]
    assert (last.combined_dx, last.combined_dy) == (2, -1)


def test_stamp_counter_rollover(stream):
    p = stream.processor
    for index in (65533, 65534, 65535, 0, 1):
        feed(p, 0, index)
    p.finalize()
    assert [offset.stamp_index for offset in p.combined_offsets] == list(
        range(65533, 65538)
    )
    assert [offset.stamp_index for offset in stream.live] == [65535, 65536]


def test_ordered_acquisitions_with_a_large_gap(stream):
    p = stream.processor
    for index in (0, 1, 40000, 40001):
        feed(p, 0, index)
    p.finalize()
    assert [offset.stamp_index for offset in p.combined_offsets] == [0, 1, 40000, 40001]


def test_retained_seed_is_independent_of_callers_buffer(stream):
    p = stream.processor
    pixels = np.array([[10, 20], [1, 1]], dtype=np.float32)
    metadata = SimpleNamespace(sensor_index=0, stamp_index=0, sequence=7)
    p.on_stamp(pixels, metadata)
    pixels[:] = 999
    feed(p, 0, 1, (10, 20))
    assert p.references == {"A": (10, 20)}


def test_new_series_refreshes_automatic_amplifier_map_and_roi():
    p = StreamingGuiderProcessor(GuiderTrackerConfig(seed_frames=10))
    for sequence, segment, startrow, label in [
        (1, 5, 10, "first"),
        (2, 17, 30, "second"),
    ]:
        p.on_stamp(
            np.zeros((3, 3)),
            SimpleNamespace(
                sensor_index=3,
                stamp_index=0,
                sequence=sequence,
                segment=segment,
                startrow=startrow,
                startcol=20,
                obs_id=label,
            ),
        )
        assert p.combiner.sensor_amplifiers == {"R00_SG0": f"C{segment:02d}"}
        assert p.sensor_rois == {"R00_SG0": (segment, startrow, 20)}
        assert p.visit_label == label


def test_explicit_amplifier_map_survives_series_transition():
    p = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=10), sensor_amplifiers={"R00_SG0": "C05"}
    )
    for sequence in (1, 2):
        p.on_stamp(
            np.zeros((3, 3)),
            SimpleNamespace(
                sensor_index=3,
                stamp_index=0,
                sequence=sequence,
                segment=17,
            ),
        )
    assert p.combiner.sensor_amplifiers == {"R00_SG0": "C05"}


def test_live_and_offline_algorithms_agree_with_real_centroiding():
    config = GuiderTrackerConfig(seed_frames=5)
    sensors = {3: "R00_SG0", 39: "R04_SG0"}
    rows, columns = np.indices((100, 140))
    camera_offsets = [(0, 0)] * 5 + [(2, -1), (0.4, -0.65), (-1.2, 2.3)]
    sequences = {}
    for sensor in sensors.values():
        stamps = []
        for index, (dx, dy) in enumerate(camera_offsets):
            lx, ly = (-dy, -dx) if sensor == "R00_SG0" else (dx, -dy)
            star = (
                100_000
                / (8 * np.pi)
                * np.exp(-((columns - 70.25 - lx) ** 2 + (rows - 45.7 - ly) ** 2) / 8)
            )
            stamps.append(
                (
                    100 + star + np.random.default_rng(index).normal(0, 5, rows.shape)
                ).astype(np.float32)
            )
        sequences[sensor] = np.stack(stamps)
    offline = MultiSensorRunner(
        list(sensors.values()), config, {name: "C05" for name in sensors.values()}
    )
    assert len(offline.lock_references(sequences)) == 2
    stream = StreamingGuiderProcessor(config)
    expected = []
    for index in range(len(camera_offsets)):
        stamps = {name: cube[index] for name, cube in sequences.items()}
        _, result = offline.process_acquisition(index, stamps)
        expected.append(result)
        for sensor_index, name in sensors.items():
            stream.on_stamp(
                stamps[name],
                SimpleNamespace(
                    sensor_index=sensor_index,
                    stamp_index=index,
                    sequence=1,
                    segment=5,
                ),
            )
    stream.finalize()
    assert stream.references == offline.references
    for actual, wanted, offset in zip(
        stream.combined_offsets, expected, camera_offsets, strict=True
    ):
        assert actual == wanted
        np.testing.assert_allclose(
            (actual.combined_dx, actual.combined_dy), offset, atol=0.05
        )
