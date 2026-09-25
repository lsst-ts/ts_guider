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
from astropy.io import fits
from lsst.ts.guider.pipeline import (
    CentroidMeasurement,
    GuiderTrackerConfig,
    MultiSensorRunner,
    SensorTracker,
    StreamingGuiderProcessor,
    build_reference_image,
    run_offline,
)
from lsst.ts.guider.pipeline import sensor_tracker as tracker_module

SHAPE = (160, 160)
REFERENCE = np.array([80.25, 80.7])


def make_stamp(offset=(0, 0), flux=100_000, seed=0):
    """Generate a noisy star with a known position and total flux."""
    rows, columns = np.indices(SHAPE)
    x, y = REFERENCE + offset
    star = flux / (8 * np.pi) * np.exp(-((columns - x) ** 2 + (rows - y) ** 2) / 8)
    noise = np.random.default_rng(seed).normal(0, 5, SHAPE)
    return (100 + star + noise).astype(np.float32)


def write_sequence(path, stamps, sensor="SG0"):
    """Write guider FITS input without an amplifier coordinate transform."""
    primary = fits.PrimaryHDU()
    primary.header["RAFTBAY"] = "R00"
    primary.header["CCDSLOT"] = sensor
    hdus = [primary]
    for index, stamp in enumerate(stamps):
        hdu = fits.ImageHDU(stamp)
        hdu.header["STMPTMJD"] = 60_000 + index / 86_400
        hdus.append(hdu)
    fits.HDUList(hdus).writeto(path)
    return path


@pytest.mark.parametrize("seed_frames", [1, 10])
def test_explicit_lock_uses_entire_cube_for_coadd(seed_frames):
    # A blank configured prefix cannot detect this star. The majority
    # of the supplied cube can, even when the configured minimum is one.
    stamps = np.stack(
        [np.zeros(SHAPE, dtype=np.float32)] * seed_frames
        + [make_stamp(seed=index) for index in range(seed_frames + 2)]
    )
    original = stamps.copy()
    tracker = SensorTracker("test", GuiderTrackerConfig(seed_frames=seed_frames))
    assert tracker.lock_reference(stamps)
    np.testing.assert_allclose(tracker.reference_center, REFERENCE, atol=0.05)
    np.testing.assert_array_equal(stamps, original)


@pytest.mark.parametrize("valid_at_end, expected", [(True, True), (False, False)])
def test_candidate_validation_uses_entire_cube(monkeypatch, valid_at_end, expected):
    # Fix detection to isolate the fraction's numerator and denominator.
    # The first ten have either 4/10 or 6/10 good measurements; all
    # twelve have 6/12 or 6/12. A stricter gate rejects the latter case.
    values = [0] * 6 + [1] * 6 if valid_at_end else [1] * 6 + [0] * 6
    fraction = 0.5 if valid_at_end else 0.6
    stamps = np.stack([np.full((2, 2), value) for value in values])
    tracker = SensorTracker(
        "test", GuiderTrackerConfig(seed_frames=10, min_valid_stamp_fraction=fraction)
    )
    measured = []

    def measure(frame, center):
        value = int(frame[0, 0])
        measured.append(value)
        return CentroidMeasurement(snr=100, passed_quality=bool(value))

    monkeypatch.setattr(
        tracker_module, "find_candidate_centroids", lambda image, config: [(1, 1, 100)]
    )
    monkeypatch.setattr(tracker, "_measure_at", measure)
    assert tracker.lock_reference(stamps) is expected
    assert measured == values


def test_reference_helper_preserves_explicit_count():
    stamps = np.full((5, 4, 4), 100, dtype=np.float32)
    stamps[:2] = 0
    reference = build_reference_image(stamps, 2)
    assert np.all((reference >= 0) & (reference < 1))
    np.testing.assert_array_equal(reference, build_reference_image(stamps, 2))


def test_real_faint_source_locks_after_initial_ten_seeds():
    config = GuiderTrackerConfig(seed_frames=10, detection_threshold=40)
    processor = StreamingGuiderProcessor(config, sensor_names={0: "test"})
    stamps = [make_stamp(flux=2_000, seed=index) for index in range(60)]
    for index, stamp in enumerate(stamps):
        processor.on_stamp(
            stamp, SimpleNamespace(sensor_index=0, stamp_index=index, sequence=1)
        )
        if index == 9:
            assert processor.references == {}
            assert len(processor.seed_buffers["test"]) == 10
        if processor.references:
            break
    assert index > 9
    assert processor.references
    assert len(processor.metrics.lock_times) == index + 2 - config.seed_frames
    np.testing.assert_allclose(processor.references["test"], REFERENCE, atol=0.3)
    assert processor.seed_buffers["test"] == []
    assert processor.seed_indices["test"] == []
    assert sorted(processor.acquisitions) == list(range(index + 1))
    tracker = processor.trackers["test"]
    for stamp_index in range(index + 1):
        actual = processor.acquisitions[stamp_index]["test"]
        expected = tracker.measure(stamps[stamp_index])
        assert actual.passed_quality == expected.passed_quality
        np.testing.assert_allclose((actual.x, actual.y), (expected.x, expected.y))


def test_explicit_runner_lock_uses_supplied_cube():
    stamps = np.stack(
        [np.zeros(SHAPE, dtype=np.float32)] * 3
        + [make_stamp(seed=index) for index in range(5)]
    )
    runner = MultiSensorRunner(["test"], GuiderTrackerConfig(seed_frames=3))
    assert runner.lock_references({"test": stamps}) == ["test"]
    np.testing.assert_allclose(runner.references["test"], REFERENCE, atol=0.05)


def test_offline_first_successful_prefix_keeps_zero_point(tmp_path):
    stamps = [make_stamp(seed=index) for index in range(5)]
    stamps += [make_stamp((8, 0), seed=index) for index in range(5, 20)]
    path = write_sequence(tmp_path / "moving.fits", stamps)
    offsets = run_offline([path], GuiderTrackerConfig(seed_frames=5))
    assert [result.stamp_index for result in offsets] == list(range(20))
    np.testing.assert_allclose(
        [(result.combined_dx, result.combined_dy) for result in offsets],
        [(0, 0)] * 5 + [(8, 0)] * 15,
        atol=0.05,
    )


def test_offline_retries_prefix_and_measures_rejected_seeds(tmp_path, monkeypatch):
    attempts = []
    original = SensorTracker.lock_reference

    def record_attempt(self, stamps):
        attempts.append(len(stamps))
        return original(self, stamps)

    monkeypatch.setattr(SensorTracker, "lock_reference", record_attempt)
    stamps = [np.zeros(SHAPE, dtype=np.float32)] * 6
    stamps += [make_stamp(seed=index) for index in range(6, 12)]
    stamps += [make_stamp((1, 0.4), seed=12), make_stamp((2, -0.5), seed=13)]
    path = write_sequence(tmp_path / "delayed.fits", stamps)
    offsets = run_offline([path], GuiderTrackerConfig(seed_frames=10))
    assert attempts == [10, 11, 12]
    assert [result.stamp_index for result in offsets] == list(range(14))
    for result in offsets[:6]:
        assert (result.n_valid, result.n_total) == (0, 1)
        assert not result.measurements["R00_SG0"].passed_quality
        assert np.isnan(result.combined_dx)
    np.testing.assert_allclose(
        [(result.combined_dx, result.combined_dy) for result in offsets[6:]],
        [(0, 0)] * 6 + [(1, 0.4), (2, -0.5)],
        atol=0.05,
    )


def test_offline_sensors_stop_at_their_first_success(tmp_path, monkeypatch):
    attempts = {"R00_SG0": [], "R00_SG1": []}
    original = SensorTracker.lock_reference

    def record_attempt(self, stamps):
        attempts[self.sensor_name].append(len(stamps))
        return original(self, stamps)

    monkeypatch.setattr(SensorTracker, "lock_reference", record_attempt)
    early = [make_stamp(seed=index) for index in range(14)]
    late = [np.zeros(SHAPE, dtype=np.float32)] * 6 + early[6:]
    paths = [
        write_sequence(tmp_path / "early.fits", early, "SG0"),
        write_sequence(tmp_path / "late.fits", late, "SG1"),
    ]
    offsets = run_offline(paths, GuiderTrackerConfig(seed_frames=10))
    assert attempts == {"R00_SG0": [10], "R00_SG1": [10, 11, 12]}
    assert len(offsets) == 14
    assert [result.n_valid for result in offsets] == [1] * 6 + [2] * 8


def test_offline_short_sequence_does_not_attempt_lock(tmp_path, monkeypatch):
    def unexpected_lock(self, stamps):
        pytest.fail("A sequence below the minimum must not attempt locking.")

    monkeypatch.setattr(SensorTracker, "lock_reference", unexpected_lock)
    path = write_sequence(tmp_path / "short.fits", [make_stamp()] * 4)
    assert run_offline([path], GuiderTrackerConfig(seed_frames=5)) == []


def test_offline_no_star_exhausts_input(tmp_path):
    path = write_sequence(tmp_path / "blank.fits", [np.zeros(SHAPE)] * 5)
    assert run_offline([path], GuiderTrackerConfig(seed_frames=3)) == []
