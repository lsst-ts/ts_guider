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

import dataclasses
from types import SimpleNamespace

import numpy as np
import pytest
from lsst.ts.guider.pipeline import (
    CentroidMeasurement,
    GuiderTrackerConfig,
    GuidingStatus,
    MeasurementState,
    SensorTracker,
)

REFERENCE = (105.25, 45.7)
SHAPE = (100, 160)


def make_star_stamp(center=REFERENCE, sigma=(2.0, 2.0), flux=100_000.0, seed=0):
    """Generate a noisy Gaussian star with known subpixel coordinates."""
    rows, columns = np.indices(SHAPE)
    x, y = center
    sigma_x, sigma_y = sigma
    star = (
        flux
        / (2 * np.pi * sigma_x * sigma_y)
        * np.exp(-0.5 * (((columns - x) / sigma_x) ** 2 + ((rows - y) / sigma_y) ** 2))
    )
    noise = np.random.default_rng(seed).normal(0, 5, SHAPE)
    return (100 + star + noise).astype(np.float32)


@pytest.fixture
def seed_stamps():
    return np.stack([make_star_stamp(seed=index) for index in range(10)])


@pytest.mark.parametrize("displacement", [(0, 0), (2, -1), (0.4, -0.65), (-1.2, 2.3)])
def test_centroid_and_local_offset(seed_stamps, displacement):
    tracker = SensorTracker("R00_SG0", GuiderTrackerConfig())
    assert tracker.lock_reference(seed_stamps)
    np.testing.assert_allclose(tracker.reference_center, REFERENCE, atol=0.05)
    reference = tracker.reference_center

    center = np.array(REFERENCE) + displacement
    measurement = tracker.measure(make_star_stamp(center=center, seed=123))
    assert measurement.state == MeasurementState.PASSED_QUALITY
    assert measurement.is_finite
    assert measurement.snr > tracker.config.min_snr
    assert measurement.fwhm == pytest.approx(2.355 * 2, abs=0.1)
    assert np.hypot(measurement.e1, measurement.e2) < 0.05
    np.testing.assert_allclose((measurement.x, measurement.y), center, atol=0.05)
    np.testing.assert_allclose(
        measurement.offset_from(tracker.reference), displacement, atol=0.05
    )
    assert tracker.reference_center == reference


def test_brighter_column_does_not_replace_star(seed_stamps):
    seed_stamps[:, :, 35:37] += 3_000
    tracker = SensorTracker("test", GuiderTrackerConfig())
    assert tracker.lock_reference(seed_stamps)
    np.testing.assert_allclose(tracker.reference_center, REFERENCE, atol=0.05)


def test_seed_validity_fraction(seed_stamps):
    seed_stamps[6:] = 0
    accepted = SensorTracker("test", GuiderTrackerConfig(min_valid_stamp_fraction=0.5))
    rejected = SensorTracker("test", GuiderTrackerConfig(min_valid_stamp_fraction=0.8))
    assert accepted.lock_reference(seed_stamps)
    assert not rejected.lock_reference(seed_stamps)
    assert rejected.reference_center is None


@pytest.mark.parametrize("kind", ["broad", "elongated", "faint", "blank", "nonfinite"])
def test_measurement_quality_rejection(seed_stamps, kind):
    tracker = SensorTracker("test", GuiderTrackerConfig(max_fwhm=6))
    assert tracker.lock_reference(seed_stamps)
    if kind == "broad":
        stamp = make_star_stamp(sigma=(4, 4))
    elif kind == "elongated":
        stamp = make_star_stamp(sigma=(4, 0.8))
    elif kind == "faint":
        stamp = make_star_stamp(flux=100)
    elif kind == "blank":
        stamp = np.zeros(SHAPE)
    else:
        stamp = make_star_stamp()
        stamp[46, 105] = np.nan
    measurement = tracker.measure(stamp)
    assert measurement.state != MeasurementState.PASSED_QUALITY
    with pytest.raises(RuntimeError, match="no usable offset"):
        tracker.get_offsets()
    if kind == "broad":
        assert measurement.state == MeasurementState.CONVERGED
        assert measurement.fwhm > tracker.config.max_fwhm
    elif kind == "elongated":
        assert measurement.state == MeasurementState.CONVERGED
        assert np.hypot(measurement.e1, measurement.e2) > tracker.config.max_ellipticity
    elif kind in ("blank", "nonfinite"):
        assert measurement.state == MeasurementState.NOT_CONVERGED


def test_failed_hsm_fit_retains_reference_without_usable_offset(
    seed_stamps, monkeypatch
):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    assert tracker.lock_reference(seed_stamps)
    reference = tracker.reference_center
    monkeypatch.setattr(
        "lsst.ts.guider.pipeline.sensor_tracker.galsim.hsm.FindAdaptiveMom",
        lambda *args, **kwargs: SimpleNamespace(error_message="Fit did not converge"),
    )
    tracker.process_stamp(make_star_stamp())
    assert tracker.last_measurement.state == MeasurementState.NOT_CONVERGED
    assert not tracker.last_measurement.is_finite
    assert tracker.get_measurements() == (tracker.last_measurement,)
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert tracker.reference_center == reference
    with pytest.raises(RuntimeError, match="no usable offset"):
        tracker.get_offsets()


@pytest.mark.parametrize(
    "position, expected",
    [
        ((80, 30), True),
        ((30, 70), False),
        ((5, 5), True),
        ((95, 30), False),
        ((30, 55), False),
    ],
)
def test_rectangular_stamp_bounds(position, expected):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    tracker.stamp_shape = (60, 100)
    measurement = CentroidMeasurement(
        x=position[0],
        y=position[1],
        snr=100,
        fwhm=4,
        e1=0,
        e2=0,
        state=MeasurementState.CONVERGED,
    )
    assert tracker._passes_quality(measurement) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"x": np.nan},
        {"y": np.inf},
        {"snr": np.nan},
        {"fwhm": np.nan},
        {"fwhm": 0},
        {"e1": np.inf},
        {"e2": np.nan},
        {"state": MeasurementState.NOT_SET},
        {"state": MeasurementState.NOT_CONVERGED},
    ],
)
def test_unusable_measurements_fail_quality(overrides):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    tracker.stamp_shape = SHAPE
    good = CentroidMeasurement(
        x=80, y=30, snr=100, fwhm=4, e1=0, e2=0, state=MeasurementState.CONVERGED
    )
    assert tracker._passes_quality(good)
    assert not tracker._passes_quality(dataclasses.replace(good, **overrides))


def test_failed_relock_clears_previous_reference(seed_stamps):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    with pytest.raises(RuntimeError, match="reference not locked"):
        tracker.measure(seed_stamps[0])
    assert tracker.lock_reference(seed_stamps)
    assert not tracker.lock_reference(np.zeros_like(seed_stamps))
    assert tracker.reference_center is None
    with pytest.raises(RuntimeError, match="reference not locked"):
        tracker.measure(seed_stamps[0])


def test_integer_and_short_seed_inputs(seed_stamps):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    original = seed_stamps[:3].astype(np.int32)
    before = original.copy()
    assert tracker.lock_reference(original)
    assert tracker.measure(original[0]).state == MeasurementState.PASSED_QUALITY
    np.testing.assert_array_equal(original, before)


@pytest.mark.parametrize(
    "shape", [(0, 100, 160), (2, 0, 160), (100, 160), (1, 2, 3, 4)]
)
def test_invalid_seed_shape(shape):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    with pytest.raises(ValueError, match="nonempty"):
        tracker.lock_reference(np.zeros(shape))


def test_changed_stamp_shape(seed_stamps):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    assert tracker.lock_reference(seed_stamps)
    with pytest.raises(ValueError, match="Expected stamp shape"):
        tracker.measure(np.zeros((160, 100)))


def test_process_stamp_matches_manual_lock_and_measure(seed_stamps):
    config = GuiderTrackerConfig()
    manual = SensorTracker("manual", config)
    tracker = SensorTracker("streamed", config)
    assert tracker.get_status() == GuidingStatus.NONE
    with pytest.raises(RuntimeError, match="no usable offset"):
        tracker.get_offsets()
    assert manual.lock_reference(seed_stamps)
    expected = tuple(manual.measure(stamp) for stamp in seed_stamps)

    for index, stamp in enumerate(seed_stamps):
        assert tracker.process_stamp(stamp) is None
        if index < config.seed_frames - 1:
            assert tracker.get_status() == GuidingStatus.LOCKING
            assert tracker.pending_seed_count == index + 1
            assert tracker.get_measurements() == ()
            assert tracker.lock_duration is None
            with pytest.raises(RuntimeError, match="no usable offset"):
                tracker.get_offsets()
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert tracker.reference_center == manual.reference_center
    assert tracker.pending_seed_count == 0
    assert tracker.get_measurements() == expected
    assert tracker.last_measurement == expected[-1]
    assert tracker.lock_duration >= 0
    assert len(tracker.measurement_durations) == config.seed_frames

    stamp = make_star_stamp(center=(107.25, 44.7), seed=42)
    tracker.process_stamp(stamp)
    assert tracker.get_measurements() == (manual.measure(stamp),)
    assert tracker.lock_duration is None
    assert len(tracker.measurement_durations) == 1
    np.testing.assert_allclose(tracker.get_offsets(), (2, -1), atol=0.05)


def test_rejected_seeds_keep_their_positions_without_losing_reference(seed_stamps):
    tracker = SensorTracker("test", GuiderTrackerConfig(seed_frames=5))
    seeds = seed_stamps[:5].copy()
    seeds[[1, 4]] = 0
    for stamp in seeds:
        tracker.process_stamp(stamp)

    batch = tracker.get_measurements()
    assert [m.state for m in batch] == [
        MeasurementState.PASSED_QUALITY,
        MeasurementState.NOT_CONVERGED,
        MeasurementState.PASSED_QUALITY,
        MeasurementState.PASSED_QUALITY,
        MeasurementState.NOT_CONVERGED,
    ]
    assert tracker.last_measurement is batch[-1]
    assert tracker.get_status() == GuidingStatus.LOCKED
    reference = tracker.reference_center
    assert reference is not None
    with pytest.raises(RuntimeError, match="no usable offset"):
        tracker.get_offsets()

    tracker.process_stamp(make_star_stamp(center=(107.25, 44.7)))
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert tracker.reference_center == reference
    assert len(tracker.get_measurements()) == 1
    assert len(batch) == 5
    np.testing.assert_allclose(tracker.get_offsets(), (2, -1), atol=0.05)


def test_rejected_live_stamps_keep_reference_without_a_loss_policy(seed_stamps):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    for stamp in seed_stamps:
        tracker.process_stamp(stamp)
    reference = tracker.reference_center
    assert tracker.get_status() == GuidingStatus.LOCKED
    for _ in range(20):
        tracker.process_stamp(np.zeros(SHAPE))
        assert tracker.get_status() == GuidingStatus.LOCKED
        assert tracker.last_measurement.state == MeasurementState.NOT_CONVERGED
        assert tracker.pending_seed_count == 0
        assert tracker.reference_center == reference
        with pytest.raises(RuntimeError, match="no usable offset"):
            tracker.get_offsets()
    tracker.process_stamp(make_star_stamp())
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert tracker.reference_center == reference
    np.testing.assert_allclose(tracker.get_offsets(), (0, 0), atol=0.05)


def test_failed_lock_retains_seeds_and_replays_all_after_success():
    tracker = SensorTracker("test", GuiderTrackerConfig(seed_frames=2))
    buffer = np.zeros(SHAPE, dtype=np.float32)
    for _ in range(2):
        tracker.process_stamp(buffer)
    assert tracker.get_status() == GuidingStatus.LOCKING
    assert tracker.pending_seed_count == 2
    assert tracker.get_measurements() == ()
    assert tracker.last_measurement is None
    assert tracker.reference_center is None
    assert tracker.last_error is None
    assert tracker.lock_duration >= 0

    buffer[:] = make_star_stamp()
    tracker.process_stamp(buffer)
    assert tracker.get_status() == GuidingStatus.LOCKING
    assert tracker.pending_seed_count == 3
    assert tracker.lock_duration >= 0
    assert tracker.get_measurements() == ()
    buffer[:] = make_star_stamp(center=(107.25, 44.7), seed=1)
    tracker.process_stamp(buffer)
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert tracker.pending_seed_count == 0
    batch = tracker.get_measurements()
    assert len(batch) == 4
    assert [m.state for m in batch] == [
        MeasurementState.NOT_CONVERGED,
        MeasurementState.NOT_CONVERGED,
        MeasurementState.PASSED_QUALITY,
        MeasurementState.PASSED_QUALITY,
    ]
    assert len(tracker.measurement_durations) == 4
    assert tracker.last_measurement is batch[-1]

    # Direct locking must also use all supplied images for both the
    # reference coadd and the candidate quality fraction.
    seeds = np.stack([np.zeros(SHAPE), np.zeros(SHAPE), make_star_stamp(), buffer])
    manual = SensorTracker("manual", tracker.config)
    assert manual.lock_reference(seeds)
    assert tracker.reference_center == manual.reference_center
    for actual, stamp in zip(batch, seeds):
        expected = manual.measure(stamp)
        np.testing.assert_allclose(
            dataclasses.astuple(actual),
            dataclasses.astuple(expected),
            rtol=0,
            atol=0,
            equal_nan=True,
        )


def test_growing_reference_stack_detects_source_after_initial_ten_seeds():
    config = GuiderTrackerConfig(seed_frames=10, detection_threshold=40.0)
    tracker = SensorTracker("test", config)
    # These stamps can be measured individually, but the conservative
    # detection threshold requires more than ten frames in the coadd.
    stamps = [make_star_stamp(flux=2_000, seed=index) for index in range(60)]
    first_attempt = config.seed_frames
    for stamp in stamps[:first_attempt]:
        tracker.process_stamp(stamp)
    assert tracker.get_status() == GuidingStatus.LOCKING
    assert tracker.pending_seed_count == config.seed_frames
    assert tracker.get_measurements() == ()

    for count, stamp in enumerate(stamps[first_attempt:], start=first_attempt + 1):
        tracker.process_stamp(stamp)
        if tracker.get_status() == GuidingStatus.LOCKED:
            break
        assert tracker.pending_seed_count == count
        assert tracker.get_measurements() == ()
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert tracker.pending_seed_count == 0
    assert len(tracker.get_measurements()) == count
    assert len(tracker.measurement_durations) == count
    assert sum(
        m.state == MeasurementState.PASSED_QUALITY for m in tracker.get_measurements()
    ) >= (config.min_valid_stamp_fraction * count)
    np.testing.assert_allclose(tracker.reference_center, REFERENCE, atol=0.3)

    tracker.process_stamp(make_star_stamp())
    assert len(tracker.get_measurements()) == 1
    assert tracker.pending_seed_count == 0
    assert tracker.lock_duration is None


def test_reset_discards_accumulated_seeds_after_failed_attempts():
    tracker = SensorTracker("test", GuiderTrackerConfig(seed_frames=2))
    for _ in range(3):
        tracker.process_stamp(np.zeros(SHAPE))
    assert tracker.pending_seed_count == 3
    assert tracker.get_status() == GuidingStatus.LOCKING

    tracker.reset()
    assert tracker.pending_seed_count == 0
    assert tracker.get_status() == GuidingStatus.NONE
    assert tracker.get_measurements() == ()
    assert tracker.lock_duration is None
    tracker.process_stamp(make_star_stamp())
    assert tracker.pending_seed_count == 1
    assert tracker.lock_duration is None
    tracker.process_stamp(make_star_stamp(seed=1))
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert len(tracker.get_measurements()) == 2


def test_buffered_seed_is_copied_and_reset_discards_it():
    tracker = SensorTracker("test", GuiderTrackerConfig(seed_frames=2))
    stamp = make_star_stamp()
    tracker.process_stamp(stamp)
    stamp[:] = 0
    tracker.process_stamp(make_star_stamp(seed=1))
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert all(
        m.state == MeasurementState.PASSED_QUALITY for m in tracker.get_measurements()
    )

    tracker.reset()
    assert tracker.get_status() == GuidingStatus.NONE
    assert tracker.reference_center is None
    assert tracker.stamp_shape is None
    assert tracker.last_measurement is None
    assert tracker.last_error is None
    assert tracker.get_measurements() == ()
    assert tracker.measurement_durations == ()
    assert tracker.lock_duration is None
    tracker.process_stamp(make_star_stamp())
    tracker.reset()
    assert tracker.pending_seed_count == 0
    tracker.process_stamp(make_star_stamp())
    assert tracker.get_status() == GuidingStatus.LOCKING
    assert tracker.pending_seed_count == 1


@pytest.mark.parametrize("shape", [(0, 160), (100, 0), (100,), (1, 100, 160)])
def test_malformed_stamp_sets_error_and_requires_reset(shape):
    tracker = SensorTracker("test", GuiderTrackerConfig(seed_frames=1))
    with pytest.raises(ValueError, match="nonempty") as caught:
        tracker.process_stamp(np.zeros(shape))
    assert tracker.get_status() == GuidingStatus.ERROR
    assert tracker.last_error is caught.value
    assert tracker.get_measurements() == ()
    with pytest.raises(RuntimeError, match="reset required"):
        tracker.process_stamp(make_star_stamp())
    tracker.reset()
    tracker.process_stamp(make_star_stamp())
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert len(tracker.get_measurements()) == 1


@pytest.mark.parametrize("locked", [False, True])
def test_shape_change_during_seeding_or_tracking_sets_error(locked):
    tracker = SensorTracker("test", GuiderTrackerConfig(seed_frames=2))
    tracker.process_stamp(make_star_stamp())
    if locked:
        tracker.process_stamp(make_star_stamp(seed=1))
    with pytest.raises(ValueError, match="Expected stamp shape"):
        tracker.process_stamp(np.zeros((160, 100)))
    assert tracker.get_status() == GuidingStatus.ERROR
    assert tracker.pending_seed_count == 0
    assert tracker.last_measurement is None
    assert tracker.get_measurements() == ()
    with pytest.raises(RuntimeError, match="no usable offset"):
        tracker.get_offsets()


@pytest.mark.parametrize("locked", [False, True])
def test_unexpected_processing_error_is_reraised(monkeypatch, locked):
    tracker = SensorTracker("test", GuiderTrackerConfig(seed_frames=1))
    if locked:
        tracker.process_stamp(make_star_stamp())
    failure = ArithmeticError("measurement failed")

    def fail(stamp, center):
        raise failure

    monkeypatch.setattr(tracker, "_measure_at", fail)
    with pytest.raises(ArithmeticError, match="measurement failed"):
        tracker.process_stamp(make_star_stamp())
    assert tracker.get_status() == GuidingStatus.ERROR
    assert tracker.last_error is failure
    assert tracker.get_measurements() == ()
    assert tracker.pending_seed_count == 0
    with pytest.raises(RuntimeError, match="reset required"):
        tracker.measure(make_star_stamp())


def test_explicit_lock_and_measure_update_the_same_status(seed_stamps):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    assert tracker.lock_reference(seed_stamps)
    assert tracker.get_status() == GuidingStatus.LOCKED
    with pytest.raises(RuntimeError, match="no usable offset"):
        tracker.get_offsets()
    tracker.measure(seed_stamps[0])
    np.testing.assert_allclose(tracker.get_offsets(), (0, 0), atol=0.05)
    tracker.measure(np.zeros(SHAPE))
    assert tracker.get_status() == GuidingStatus.LOCKED
    with pytest.raises(RuntimeError, match="no usable offset"):
        tracker.get_offsets()
    with pytest.raises(ValueError):
        tracker.measure(np.zeros((3, 3)))
    assert tracker.get_status() == GuidingStatus.ERROR
    assert tracker.lock_reference(seed_stamps)
    assert tracker.last_error is None
    assert tracker.last_measurement is None
    assert tracker.get_measurements() == ()
    tracker.process_stamp(seed_stamps[0])
    assert tracker.get_status() == GuidingStatus.LOCKED


def test_reference_has_detected_coordinates_without_invented_measurement_fields(
    seed_stamps,
):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    assert tracker.lock_reference(seed_stamps)
    assert isinstance(tracker.reference, CentroidMeasurement)
    assert tracker.reference.is_finite
    np.testing.assert_allclose(tracker.reference_center, REFERENCE, atol=0.05)
    assert tracker.reference.state == MeasurementState.NOT_SET
    assert tracker.reference.snr == 0
    assert np.isnan(tracker.reference.fwhm)
    tracker.measure(seed_stamps[0])
    np.testing.assert_allclose(tracker.get_offsets(), (0, 0), atol=0.05)


def test_configuration_changes_apply_to_next_measurement(seed_stamps):
    config = GuiderTrackerConfig()
    tracker = SensorTracker("test", config)
    assert tracker.lock_reference(seed_stamps)
    reference = tracker.reference
    stamp = make_star_stamp()
    tracker.process_stamp(stamp)
    assert tracker.last_measurement.state == MeasurementState.PASSED_QUALITY

    config.min_snr = 2 * tracker.last_measurement.snr
    tracker.process_stamp(stamp)
    assert tracker.last_measurement.state == MeasurementState.CONVERGED
    assert tracker.get_status() == GuidingStatus.LOCKED
    assert tracker.reference is reference
    assert tracker.config is config
    assert tracker.detection.config is config
    with pytest.raises(RuntimeError, match="no usable offset"):
        tracker.get_offsets()


@pytest.mark.parametrize("locked", [False, True])
def test_reconfiguration_resets_seeds_reference_and_detector(locked):
    config = GuiderTrackerConfig(seed_frames=2)
    tracker = SensorTracker("test", config)
    tracker.process_stamp(make_star_stamp())
    if locked:
        tracker.process_stamp(make_star_stamp(seed=1))
    replacement = dataclasses.replace(config, seed_frames=1, min_snr=20)
    tracker.reset(config=replacement)
    assert tracker.get_status() == GuidingStatus.NONE
    assert tracker.pending_seed_count == 0
    assert tracker.reference is None
    assert tracker.get_measurements() == ()
    assert tracker.config is replacement
    assert tracker.detection.config is replacement
    assert config.seed_frames == 2
    tracker.process_stamp(make_star_stamp(center=(110, 45)))
    assert tracker.get_status() == GuidingStatus.LOCKED
    np.testing.assert_allclose(tracker.reference_center, (110, 45), atol=0.05)
    tracker.reset()
    assert tracker.config is replacement
