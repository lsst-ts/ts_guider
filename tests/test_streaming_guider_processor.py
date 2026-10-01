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
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits
from lsst.ts.guider.pipeline import (
    CentroidMeasurement,
    Detection,
    GuiderTrackerConfig,
    GuidingStatus,
    MeasurementState,
    SensorTracker,
    StreamingGuiderProcessor,
    run_offline,
)


@pytest.fixture
def stream(monkeypatch):
    """Use known centroids to isolate stream ordering from image fitting."""
    module = importlib.import_module(
        "lsst.ts.guider.pipeline.streaming_guider_processor"
    )

    lock_after = {"A": 2, "B": 2}

    class Tracker(SensorTracker):
        """Keep the real tracker lifecycle with deterministic image results."""

        def __init__(self, name, config):
            super().__init__(name, config)
            self.lock_sizes = []
            self.measured = []
            self.recorded_durations = []
            self.recorded_lock_times = []

        def _lock_reference(self, stamps):
            self.lock_sizes.append(len(stamps))
            if len(stamps) < lock_after.get(self.sensor_name, 2):
                return False
            usable = [stamp for stamp in stamps if stamp[0, 0] >= 0]
            if not usable:
                return False
            self.reference = CentroidMeasurement(
                x=float(usable[0][0, 0]), y=float(usable[0][0, 1])
            )
            return True

        def _measure_at(self, stamp, center):
            self.measured.append(stamp.copy())
            return CentroidMeasurement(
                x=float(stamp[0, 0]),
                y=float(stamp[0, 1]),
                snr=100,
                fwhm=4,
                e1=0,
                e2=0,
                state=MeasurementState(int(stamp[1, 0])),
            )

        def process_stamp(self, stamp):
            super().process_stamp(stamp)
            self.recorded_durations.extend(self.measurement_durations)
            if self.lock_duration is not None:
                self.recorded_lock_times.append(self.lock_duration)

    class Orientation:
        def amplifier_to_camera_offset(self, dx, dy, sensor, amplifier):
            return dx, dy

    monkeypatch.setattr(module, "SensorTracker", Tracker)
    live, starts, visits = [], [], []
    processor = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=2),
        sensor_names={0: "A", 1: "B"},
        sensor_amplifiers={"A": "C05", "B": "C17"},
        orientation=Orientation(),
        on_combined_offset=live.append,
        on_visit_start=lambda sequence, label: starts.append((sequence, label)),
        on_visit_complete=lambda sequence, offsets: visits.append((sequence, offsets)),
    )
    return SimpleNamespace(
        processor=processor,
        live=live,
        starts=starts,
        visits=visits,
        lock_after=lock_after,
        Orientation=Orientation,
    )


def feed(
    processor,
    sensor,
    index,
    xy=(0, 0),
    sequence=7,
    state=MeasurementState.PASSED_QUALITY,
    **metadata,
):
    """Deliver a stamp encoding a centroid and measurement state."""
    stamp = np.array([xy, (state, 1)], dtype=np.float32)
    fields = dict(
        sensor_index=sensor,
        stamp_index=index,
        sequence=sequence,
        segment=None,
        startrow=0,
        startcol=0,
        obs_id="image",
        series_id="roi",
    )
    fields.update(metadata)
    processor.on_stamp(stamp, SimpleNamespace(**fields))


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
    visit_results = stream.visits[0][1]
    assert visit_results is not p.combined_offsets
    assert all(
        visit is retained
        for visit, retained in zip(visit_results, p.combined_offsets, strict=True)
    )
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


def test_failed_lock_retries_with_each_larger_prefix(stream):
    p = stream.processor
    p.config.seed_frames = 10
    stream.lock_after["A"] = 12
    indices = [40 + 2 * i for i in range(12)]
    for index in indices[:11]:
        feed(p, 0, index, (10, 20))
    tracker = p.trackers["A"]
    assert tracker.lock_sizes == [10, 11]
    assert not p.references
    assert p.seed_indices["A"] == indices[:11]
    assert tracker.pending_seed_count == 11
    feed(p, 0, indices[-1], (11, 18))
    assert tracker.lock_sizes == [10, 11, 12]
    assert len(tracker.measured) == 12
    assert tracker.pending_seed_count == 0
    assert not p.seed_indices
    assert stream.live == []
    assert len(p.metrics.lock_times) == 3
    assert p.metrics.lock_times == tracker.recorded_lock_times
    assert p.metrics.measure_times == tracker.recorded_durations
    p.finalize()
    assert [offset.stamp_index for offset in p.combined_offsets] == indices
    last = p.combined_offsets[-1]
    assert (last.combined_dx, last.combined_dy) == (1, -2)


def test_empty_and_short_series_finalize_once_without_locking(stream):
    p = stream.processor
    p.finalize()
    assert stream.visits == []
    feed(p, 0, 0)
    p.finalize()
    p.finalize()
    assert stream.visits == [(7, [])]
    assert not p.references
    assert not p.seed_indices
    assert p.trackers["A"].pending_seed_count == 0
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
    assert set(p.references) == {"A"}
    assert (p.references["A"].x, p.references["A"].y) == (100, 200)
    assert p.references["A"].state == MeasurementState.NOT_SET
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
    pixels = np.array(
        [[10, 20], [MeasurementState.PASSED_QUALITY, 1]], dtype=np.float32
    )
    metadata = SimpleNamespace(sensor_index=0, stamp_index=0, sequence=7)
    p.on_stamp(pixels, metadata)
    pixels[:] = 999
    feed(p, 0, 1, (10, 20))
    assert set(p.references) == {"A"}
    assert (p.references["A"].x, p.references["A"].y) == (10, 20)
    assert p.references["A"].state == MeasurementState.NOT_SET
    assert p.trackers["A"].measured[0][0, 0] == 10


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


def test_live_and_offline_algorithms_agree_with_real_centroiding(tmp_path):
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
    paths = []
    for name, cube in sequences.items():
        raft, sensor = name.split("_")
        primary = fits.PrimaryHDU()
        primary.header["RAFTBAY"] = raft
        primary.header["CCDSLOT"] = sensor
        primary.header["ROISEG"] = "Segment05"
        path = tmp_path / f"{name}.fits"
        fits.HDUList([primary] + [fits.ImageHDU(stamp) for stamp in cube]).writeto(path)
        paths.append(path)
    offline = run_offline(paths, config)
    stream = StreamingGuiderProcessor(config)
    for index in range(len(camera_offsets)):
        for sensor_index, name in sensors.items():
            stream.on_stamp(
                sequences[name][index],
                SimpleNamespace(
                    sensor_index=sensor_index, stamp_index=index, sequence=1, segment=5
                ),
            )
    stream.finalize()
    for actual, wanted, offset in zip(
        stream.combined_offsets, offline, camera_offsets, strict=True
    ):
        assert actual == wanted
        np.testing.assert_allclose(
            (actual.combined_dx, actual.combined_dy), offset, atol=0.05
        )
    assert all(
        reference.state == MeasurementState.NOT_SET
        for reference in stream.references.values()
    )


@pytest.mark.parametrize("state", list(MeasurementState))
def test_measurement_states_retain_rejects_and_coordinate_references(
    stream, state, caplog
):
    p = stream.processor
    caplog.set_level(
        logging.DEBUG, logger="lsst.ts.guider.pipeline.streaming_guider_processor"
    )
    feed(p, 0, 0, (10, 20))
    xy = (12, 19) if state == MeasurementState.PASSED_QUALITY else (np.nan, np.nan)
    feed(p, 0, 1, xy, state=state)
    reference = p.references["A"]
    assert reference.state == MeasurementState.NOT_SET
    assert p.trackers["A"].get_status() == GuidingStatus.LOCKED
    feed(p, 0, 2, xy, state=state)
    assert p.references["A"] is reference
    p.finalize()
    result = p.combined_offsets[-1]
    assert result.measurements["A"].state == state
    assert result.n_total == 1
    assert result.n_valid == (state == MeasurementState.PASSED_QUALITY)
    assert np.isnan(result.error_dx)
    if state == MeasurementState.PASSED_QUALITY:
        assert (result.combined_dx, result.combined_dy) == (2, -1)
        assert p.metrics.valid_measurements == 3
    else:
        assert np.isnan(result.combined_dx)
        assert p.metrics.valid_measurements == 1
        assert p.metrics.invalid_measurements == 2
        assert "reject" in caplog.text
    assert len(p.metrics.measure_times) == 3


def test_late_rejected_seed_counts_once_without_revising_emitted_result(stream):
    p = stream.processor
    for index in range(3):
        feed(p, 0, index)
    feed(p, 1, 2, state=MeasurementState.NOT_CONVERGED)
    feed(p, 0, 3)
    emitted = stream.live[0]
    feed(p, 1, 3)
    assert emitted.n_total == 1
    assert p.metrics.discarded_seed_measurements == 1
    assert (p.metrics.valid_measurements, p.metrics.invalid_measurements) == (5, 1)
    assert len(p.metrics.measure_times) == 6
    assert len(p.trackers["B"].measured) == 2
    before = (
        p.metrics.valid_measurements,
        p.metrics.invalid_measurements,
        list(p.metrics.measure_times),
    )
    p.trackers["B"].get_measurements()
    p.trackers["B"].get_measurements()
    p.finalize()
    p.finalize()
    assert before == (
        p.metrics.valid_measurements,
        p.metrics.invalid_measurements,
        p.metrics.measure_times,
    )
    assert [offset.stamp_index for offset in stream.live] == [2]


@pytest.mark.parametrize("mapping", [{}, {"A": "C05"}])
def test_explicit_maps_are_never_filled_from_metadata(stream, mapping):
    p = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=10),
        sensor_names={0: "A", 1: "B"},
        sensor_amplifiers=mapping,
        orientation=stream.Orientation(),
    )
    expected = dict(mapping)
    for sequence in (1, 2):
        feed(p, 0, 0, sequence=sequence, segment=17)
        feed(p, 1, 0, sequence=sequence, segment=5)
        assert p.combiner.sensor_amplifiers is mapping
        assert mapping == expected


def test_setter_selects_explicit_map_and_preserves_it_across_series(stream):
    p = stream.processor
    mapping = {}
    p.set_sensor_amplifiers(mapping)
    for sequence in (1, 2):
        feed(p, 0, 0, sequence=sequence, segment=17)
        assert p.combiner.sensor_amplifiers is mapping
        assert mapping == {}


def test_default_mapping_uses_two_sensors_and_clears_for_new_series():
    p = StreamingGuiderProcessor(GuiderTrackerConfig(seed_frames=10))
    for sensor, segment in ((3, 5), (39, 17)):
        feed(p, sensor, 0, segment=segment)
    assert p.combiner.sensor_amplifiers == {"R00_SG0": "C05", "R04_SG0": "C17"}
    feed(p, 3, 1, segment=17)
    assert p.combiner.sensor_amplifiers["R00_SG0"] == "C05"
    feed(p, 3, 0, sequence=8, segment=17)
    assert p.combiner.sensor_amplifiers == {"R00_SG0": "C17"}


@pytest.mark.parametrize(
    "state", [MeasurementState.PASSED_QUALITY, MeasurementState.NOT_CONVERGED]
)
def test_missing_map_entry_is_required_only_for_accepted_measurements(stream, state):
    p = stream.processor
    p.set_sensor_amplifiers({})
    for index in range(2):
        feed(p, 0, index, state=state)
    if state == MeasurementState.PASSED_QUALITY:
        with pytest.raises(KeyError, match="A"):
            p.finalize()
    else:
        p.finalize()
        assert all(
            result.n_total == 1 and result.n_valid == 0 for result in p.combined_offsets
        )


def test_camera_exception_propagates_unchanged(stream):
    p = stream.processor
    error = ValueError("camera transform failed")

    def fail(*args):
        raise error

    p.combiner.orientation.amplifier_to_camera_offset = fail
    for index in range(2):
        feed(p, 0, index)
    with pytest.raises(ValueError) as caught:
        p.finalize()
    assert caught.value is error
    assert p.combined_offsets == []
    assert p.metrics.combine_times == []


def test_accepted_nonfinite_coordinates_raise_from_combiner(stream, caplog):
    caplog.set_level(
        logging.DEBUG, logger="lsst.ts.guider.pipeline.streaming_guider_processor"
    )
    p = stream.processor
    feed(p, 0, 0)
    feed(p, 0, 1)
    feed(p, 0, 2, (np.nan, 0))
    with pytest.raises(ValueError, match="non-finite"):
        feed(p, 0, 3)


def test_failed_lock_series_releases_seeds_without_final_lock(stream):
    p = stream.processor
    stream.lock_after["A"] = 99
    for index in range(4):
        feed(p, 0, index)
    tracker = p.trackers["A"]
    assert tracker.lock_sizes == [2, 3, 4]
    p.finalize()
    assert tracker.lock_sizes == [2, 3, 4]
    assert tracker.pending_seed_count == 0
    assert p.seed_indices == {}
    assert stream.visits == [(7, [])]


def test_finalization_preserves_locked_reference_and_releases_other_sensor(stream):
    p = stream.processor
    feed(p, 0, 0)
    feed(p, 1, 0)
    feed(p, 0, 1)
    reference = p.references["A"]
    p.finalize()
    assert p.references["A"] is reference
    assert p.trackers["B"].pending_seed_count == 0
    assert not p.seed_indices


def test_seed_replay_preserves_nonzero_gapped_rollover_indices(stream):
    p = stream.processor
    p.config.seed_frames = 3
    for index in (65533, 65535, 1):
        feed(p, 0, index)
    assert set(p.acquisitions) == {65533, 65535, 65537}
    assert stream.live == []
    p.finalize()
    assert [result.stamp_index for result in p.combined_offsets] == [
        65533,
        65535,
        65537,
    ]


def test_observation_label_and_roi_change_do_not_reseed(stream, caplog):
    p = stream.processor
    for index in range(2):
        feed(p, 0, index, segment=5, startrow=10, startcol=20)
    reference = p.references["A"]
    feed(p, 0, 2, obs_id="second-image", segment=17, startrow=30, startcol=40)
    assert p.references["A"] is reference
    assert p.sensor_rois["A"] == (17, 30, 40)
    assert "ROI changed" in caplog.text
    assert stream.starts == [(7, "image")]
    assert len(p.metrics.lock_times) == 1
    feed(
        p,
        0,
        0,
        sequence=8,
        obs_id="",
        series_id="next-series",
        segment=17,
        startrow=30,
        startcol=40,
    )
    assert not p.references
    assert stream.starts[-1] == (8, "next-series")


def test_configuration_is_shared_and_changes_next_seed_threshold(stream):
    p = stream.processor
    feed(p, 0, 0)
    assert p.trackers["A"].config is p.config
    p.config.seed_frames = 3
    feed(p, 0, 1)
    assert not p.references
    feed(p, 0, 2)
    assert p.trackers["A"].lock_sizes == [3]


@pytest.mark.parametrize(
    "callback", ["on_visit_start", "on_combined_offset", "on_visit_complete"]
)
def test_callback_errors_propagate_and_are_timed(stream, callback):
    p = stream.processor
    error = RuntimeError(callback)

    def fail(*args):
        raise error

    setattr(p, callback, fail)
    with pytest.raises(RuntimeError) as caught:
        for index in range(4):
            feed(p, 0, index)
        p.finalize()
    assert caught.value is error
    assert len(p.metrics.callback_times) == p.metrics.total_stamps
    if callback == "on_combined_offset":
        assert len(p.metrics.combine_times) == 1
        assert p.combined_offsets[0].stamp_index == 2


def test_tracker_error_is_latched_and_progress_does_not_replace_it(stream, monkeypatch):
    p = stream.processor
    feed(p, 0, 0)
    tracker = p.trackers["A"]
    error = ValueError("reference selection failed unexpectedly")

    def fail(*args):
        raise error

    monkeypatch.setattr(tracker, "_lock_reference", fail)
    monkeypatch.setattr(
        p, "_log_progress", lambda: (_ for _ in ()).throw(RuntimeError("progress"))
    )
    p.progress_interval = 1
    with pytest.raises(ValueError) as caught:
        feed(p, 0, 1)
    assert caught.value is error
    assert tracker.get_status() == GuidingStatus.ERROR
    assert len(p.metrics.lock_times) == 1
    with pytest.raises(RuntimeError, match="reset required") as caught:
        feed(p, 0, 2)
    assert caught.value.__cause__ is error
    assert len(p.metrics.lock_times) == 1
    assert p.metrics.measure_times == []
    assert len(p.metrics.callback_times) == 3


@pytest.mark.parametrize("phase", ["seed", "live"])
@pytest.mark.parametrize("mismatch", ["measurements", "durations"])
def test_tracker_count_mismatch_raises_instead_of_truncating(
    stream, monkeypatch, phase, mismatch
):
    p = stream.processor
    feed(p, 0, 10)
    if phase == "live":
        feed(p, 0, 12)
    tracker = p.trackers["A"]
    process = tracker.process_stamp
    pending = {index: dict(results) for index, results in p.acquisitions.items()}
    durations = list(p.metrics.measure_times)

    def broken(stamp):
        process(stamp)
        tracker.measurement_durations = tracker.measurement_durations[:-1]
        if mismatch == "measurements":
            tracker._measurements = tracker.get_measurements()[:-1]

    monkeypatch.setattr(tracker, "process_stamp", broken)
    with pytest.raises(RuntimeError, match="counts differ"):
        feed(p, 0, 14)
    assert p.acquisitions == pending
    assert p.metrics.measure_times == durations


def test_tracker_measurements_without_a_reference_raise(stream, monkeypatch):
    p = stream.processor
    feed(p, 0, 10)
    tracker = p.trackers["A"]

    def broken(stamp):
        # Simulate an inconsistent tracker result at the processor boundary.
        tracker._measurements = (CentroidMeasurement(x=0, y=0),)
        tracker.measurement_durations = (0.01,)

    monkeypatch.setattr(tracker, "process_stamp", broken)
    with pytest.raises(RuntimeError, match="measurements without a reference"):
        feed(p, 0, 12)
    assert p.acquisitions == {}
    assert p.metrics.measure_times == []


def test_finalized_stamps_count_but_do_not_repeat_results_or_completion(stream):
    p = stream.processor
    feed(p, 0, 0)
    feed(p, 0, 1)
    p.finalize()
    measures = list(p.metrics.measure_times)
    combines = list(p.metrics.combine_times)
    for index in (2, 3):
        feed(p, 0, index)
    p.finalize()
    assert p.metrics.total_stamps == 4
    assert len(p.metrics.callback_times) == 4
    assert p.metrics.measure_times == measures
    assert p.metrics.combine_times == combines
    assert len(stream.visits) == 1
    assert p.metrics.stream_seconds > 0


def test_reference_dictionary_is_new_but_values_are_shared(stream):
    p = stream.processor
    feed(p, 0, 0)
    feed(p, 0, 1)
    first, second = p.references, p.references
    assert first is not second
    assert first["A"] is second["A"] is p.trackers["A"].reference
    del first["A"]
    assert "A" in p.references


def test_sensor_name_override_and_diagnostic_fallback():
    p = StreamingGuiderProcessor(GuiderTrackerConfig(), sensor_names={0: "R00_SG0"})
    assert p._sensor_name(0) == "R00_SG0"
    assert p._sensor_name(3) == "R00_SG0"
    assert p._sensor_name(6) == "sensor_06"


def make_real_stamp(offset=(0, 0), seed=0):
    """Return a noisy image with a star at a known amplifier position."""
    rows, columns = np.indices((100, 140))
    dx, dy = offset
    star = (
        100_000
        / (8 * np.pi)
        * np.exp(-((columns - 70.25 - dx) ** 2 + (rows - 45.7 - dy) ** 2) / 8)
    )
    return np.rint(
        100 + star + np.random.default_rng(seed).normal(0, 5, rows.shape)
    ).astype(np.int32)


def write_real_sequence(path, sensor_name, stamps):
    """Write lossless image extensions with the parent reader's metadata."""
    primary = fits.PrimaryHDU()
    primary.header["RAFTBAY"], primary.header["CCDSLOT"] = sensor_name.split("_")
    primary.header["ROISEG"] = "Segment05"
    fits.HDUList([primary] + [fits.ImageHDU(stamp) for stamp in stamps]).writeto(path)
    return path


def test_real_tracker_expands_failed_prefix_and_retains_rejected_seed_results(
    monkeypatch, caplog
):
    attempts = []
    build_reference = Detection.build_reference_image

    def record_prefix(self, stamps):
        attempts.append(len(stamps))
        return build_reference(self, stamps)

    monkeypatch.setattr(Detection, "build_reference_image", record_prefix)
    caplog.set_level(
        logging.DEBUG, logger="lsst.ts.guider.pipeline.streaming_guider_processor"
    )
    p = StreamingGuiderProcessor(GuiderTrackerConfig(seed_frames=10))
    stamps = [np.zeros((100, 140), dtype=np.int32)] * 6
    stamps += [make_real_stamp(seed=i) for i in range(6, 12)]
    stamps += [make_real_stamp((1, 0.4), 12), make_real_stamp((2, -0.5), 13)]
    for index, stamp in enumerate(stamps):
        p.on_stamp(
            stamp,
            SimpleNamespace(sensor_index=39, stamp_index=index, sequence=7, segment=5),
        )
    p.finalize()
    assert attempts == [10, 11, 12]
    assert len(p.metrics.lock_times) == 3
    assert len(p.metrics.measure_times) == 14
    assert (p.metrics.valid_measurements, p.metrics.invalid_measurements) == (8, 6)
    assert [r.stamp_index for r in p.combined_offsets] == list(range(14))
    for result in p.combined_offsets[:6]:
        assert (result.n_total, result.n_valid) == (1, 0)
        assert result.measurements["R04_SG0"].state == MeasurementState.NOT_CONVERGED
        assert np.isnan(result.combined_dx)
    np.testing.assert_allclose(
        [(r.combined_dx, r.combined_dy) for r in p.combined_offsets[6:]],
        [(0, 0)] * 6 + [(1, -0.4), (2, 0.5)],
        atol=0.05,
    )


def test_real_late_lock_cannot_revise_live_acquisition_but_offline_can(tmp_path):
    config = GuiderTrackerConfig(seed_frames=10)
    sequences = {
        "R00_SG0": [make_real_stamp(seed=i) for i in range(14)],
        "R04_SG0": [np.zeros((100, 140), dtype=np.int32)] * 6
        + [make_real_stamp(seed=i) for i in range(6, 14)],
    }
    paths = [
        write_real_sequence(tmp_path / f"{name}.fits", name, stamps)
        for name, stamps in sequences.items()
    ]
    offline = run_offline(paths, config)
    live = []
    p = StreamingGuiderProcessor(config, on_combined_offset=live.append)
    for index in range(14):
        for sensor, name in ((3, "R00_SG0"), (39, "R04_SG0")):
            p.on_stamp(
                sequences[name][index],
                SimpleNamespace(
                    sensor_index=sensor, stamp_index=index, sequence=7, segment=5
                ),
            )
    p.finalize()
    assert [r.stamp_index for r in live] == [10, 11, 12]
    assert live[0].n_total == live[0].n_valid == 1
    assert offline[10].n_total == offline[10].n_valid == 2
    assert p.metrics.discarded_seed_measurements == 1
    assert p.metrics.valid_measurements + p.metrics.invalid_measurements == 28
    assert len(p.metrics.measure_times) == 28
    assert p.combined_offsets[10] is live[0]
    assert p.combined_offsets[11].n_total == 2


def test_combine_timer_excludes_result_callback(stream, monkeypatch):
    p = stream.processor
    feed(p, 0, 0)
    feed(p, 0, 1)
    feed(p, 0, 2)
    clock = [0.0]
    module = importlib.import_module(
        "lsst.ts.guider.pipeline.streaming_guider_processor"
    )
    monkeypatch.setattr(module, "perf_counter", lambda: clock[0])
    combine = p.combiner.combine

    def timed_combine(*args):
        result = combine(*args)
        clock[0] += 0.25
        return result

    def slow_callback(result):
        assert result.stamp_index == 2
        clock[0] += 3

    monkeypatch.setattr(p.combiner, "combine", timed_combine)
    p.on_combined_offset = slow_callback
    feed(p, 0, 3)
    assert p.metrics.combine_times == [0.25]
    assert p.metrics.callback_times[-1] == 3.25
    assert clock[0] == 3.25


@pytest.mark.parametrize("old,new", [(7, 8), (65535, 0)])
@pytest.mark.parametrize("finalize_first", [False, True])
def test_visit_start_once_after_metadata_and_previous_completion(
    stream, old, new, finalize_first
):
    p = stream.processor
    events = []

    def started(sequence, label):
        events.append(("start", sequence, label))
        assert p.visit_label == label
        assert p.sensor_rois == {"A": (5, 0, 0)}

    p.on_visit_start = started
    p.on_visit_complete = lambda sequence, offsets: events.append(
        ("complete", sequence)
    )
    for index in (0, 1):
        for sensor in (0, 1):
            feed(p, sensor, index, sequence=old, segment=5, obs_id="first")
    assert events == [("start", old, "first")]
    if finalize_first:
        p.finalize()
        p.finalize()
        feed(p, 0, 2, sequence=old, segment=5, obs_id="first")
        assert events == [("start", old, "first"), ("complete", old)]

    for sensor in (0, 1):
        feed(p, sensor, 0, sequence=new, segment=5, obs_id="second")
    assert events == [
        ("start", old, "first"),
        ("complete", old),
        ("start", new, "second"),
    ]
    p.finalize()
    assert events[-1] == ("complete", new)


def test_visit_start_does_not_wait_for_or_repeat_on_later_label(stream):
    p = stream.processor
    feed(p, 0, 0, obs_id="", series_id="")
    assert stream.starts == [(7, "")]
    feed(p, 1, 0, obs_id="late-label")
    assert p.visit_label == "late-label"
    assert stream.starts == [(7, "")]
    feed(p, 0, 0, sequence=8, obs_id="", series_id="next-series")
    assert stream.starts == [(7, ""), (8, "next-series")]
