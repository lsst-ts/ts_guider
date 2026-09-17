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

import numpy as np
import pytest
from lsst.ts.guider.pipeline import (
    CentroidMeasurement,
    GuiderTrackerConfig,
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
    assert measurement.converged
    assert measurement.passed_quality
    assert measurement.is_finite
    assert measurement.snr > tracker.config.min_snr
    assert measurement.fwhm == pytest.approx(2.355 * 2, abs=0.1)
    assert np.hypot(measurement.e1, measurement.e2) < 0.05
    np.testing.assert_allclose((measurement.x, measurement.y), center, atol=0.05)
    np.testing.assert_allclose(
        measurement.offset_from(reference), displacement, atol=0.05
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
    assert not measurement.passed_quality
    if kind == "broad":
        assert measurement.converged
        assert measurement.fwhm > tracker.config.max_fwhm
    elif kind == "elongated":
        assert measurement.converged
        assert np.hypot(measurement.e1, measurement.e2) > tracker.config.max_ellipticity


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
        x=position[0], y=position[1], snr=100, fwhm=4, e1=0, e2=0, converged=True
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
        {"converged": False},
    ],
)
def test_unusable_measurements_fail_quality(overrides):
    tracker = SensorTracker("test", GuiderTrackerConfig())
    tracker.stamp_shape = SHAPE
    good = CentroidMeasurement(x=80, y=30, snr=100, fwhm=4, e1=0, e2=0, converged=True)
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
    assert tracker.measure(original[0]).passed_quality
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
