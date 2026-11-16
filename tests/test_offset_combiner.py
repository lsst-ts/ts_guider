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

import math

import numpy as np
import pytest
from lsst.ts.guider.pipeline import (
    CentroidMeasurement,
    GuiderSequence,
    MeasurementState,
    OffsetCombiner,
    build_sensor_amplifiers,
)


def measurement(x, y, *, accepted=True):
    return CentroidMeasurement(
        x=x,
        y=y,
        snr=100,
        fwhm=4,
        e1=0,
        e2=0,
        state=(
            MeasurementState.PASSED_QUALITY if accepted else MeasurementState.CONVERGED
        ),
    )


def test_camera_mean_scatter_error_and_preserved_measurements():
    # R00/C05 maps (dx, dy) -> (-dy, -dx); R04/C05 maps -> (dx, -dy).
    # These local displacements therefore become camera offsets (2, -1)
    # and (4, 1), with mean (3, 0), sample scatter (sqrt(2), sqrt(2)),
    # and standard error (1, 1).
    measurements = {
        "R00_SG0": measurement(101, 78),
        "R04_SG0": measurement(204, 119),
        "rejected": measurement(999, 999, accepted=False),
        "no_reference": measurement(999, 999),
    }
    references = {
        "R00_SG0": CentroidMeasurement(x=100, y=80),
        "R04_SG0": CentroidMeasurement(x=200, y=120),
        "rejected": CentroidMeasurement(x=0, y=0),
    }
    result = OffsetCombiner({"R00_SG0": "C05", "R04_SG0": "C05"}).combine(
        42, measurements, references
    )
    assert result.stamp_index == 42
    assert (result.combined_dx, result.combined_dy) == (3, 0)
    assert (result.n_valid, result.n_total) == (2, 4)
    np.testing.assert_allclose((result.scatter_dx, result.scatter_dy), np.sqrt(2))
    np.testing.assert_allclose((result.error_dx, result.error_dy), (1, 1))
    assert result.per_sensor_offset == {"R00_SG0": (2, -1), "R04_SG0": (4, 1)}
    assert result.measurements == measurements
    assert result.measurements is not measurements
    measurements.clear()
    assert len(result.measurements) == 4
    assert result.measurements["rejected"].state == MeasurementState.CONVERGED


def test_one_valid_sensor_has_no_scatter_estimate():
    result = OffsetCombiner({"R00_SG0": "C05"}).combine(
        5,
        {"R00_SG0": measurement(11, 18)},
        {"R00_SG0": CentroidMeasurement(x=10, y=20)},
    )
    assert (result.combined_dx, result.combined_dy) == (2, -1)
    assert (result.n_valid, result.n_total) == (1, 1)
    assert all(
        math.isnan(v)
        for v in (
            result.scatter_dx,
            result.scatter_dy,
            result.error_dx,
            result.error_dy,
        )
    )


@pytest.mark.parametrize(
    "measurements", [{}, {"R00_SG0": measurement(100, 80, accepted=False)}]
)
def test_no_valid_offsets_retains_diagnostics(measurements):
    result = OffsetCombiner({"R00_SG0": "C05"}).combine(
        0, measurements, {"R00_SG0": CentroidMeasurement(x=100, y=80)}
    )
    assert result.n_valid == 0
    assert result.n_total == len(measurements)
    assert result.measurements == measurements
    assert result.measurements is not measurements
    assert result.per_sensor_offset == {}
    assert all(
        math.isnan(v)
        for v in (
            result.combined_dx,
            result.combined_dy,
            result.scatter_dx,
            result.error_dx,
        )
    )


def test_original_unmapped_mode_averages_amplifier_offsets():
    result = OffsetCombiner().combine(
        0,
        {"a": measurement(12, 19), "b": measurement(24, 31)},
        {"a": CentroidMeasurement(x=10, y=20), "b": CentroidMeasurement(x=20, y=30)},
    )
    assert (result.combined_dx, result.combined_dy) == (3, 0)


def test_build_amplifier_map_uses_fits_segment():
    stamps = np.zeros((1, 60, 60))
    sequences = {
        name: GuiderSequence(stamps, np.array([60000.0]), name, segment)
        for name, segment in [("a", "Segment05"), ("b", "Segment17")]
    }
    assert build_sensor_amplifiers(sequences) == {"a": "C05", "b": "C17"}


@pytest.mark.parametrize("state", list(MeasurementState))
def test_only_passed_quality_contributes(state):
    sample = measurement(2, -1)
    sample.state = state
    reference = CentroidMeasurement(x=0, y=0)
    assert reference.state == MeasurementState.NOT_SET
    result = OffsetCombiner().combine(0, {"a": sample}, {"a": reference})
    assert result.n_valid == int(state == MeasurementState.PASSED_QUALITY)
    assert result.n_total == 1
    assert result.measurements["a"] is sample


@pytest.mark.parametrize("amplifiers", [{}, {"R00_SG0": "C05"}])
def test_camera_mode_rejects_missing_amplifier_instead_of_mixing_frames(amplifiers):
    # Both local offsets describe (2, -1) in the camera frame. The old
    # partial-map fallback silently returned (2, 0) with two contributors.
    samples = {"R00_SG0": measurement(1, -2), "R04_SG0": measurement(2, 1)}
    references = {name: CentroidMeasurement(x=0, y=0) for name in samples}
    complete = OffsetCombiner({name: "C05" for name in samples}).combine(
        0, samples, references
    )
    assert (complete.combined_dx, complete.combined_dy) == (2, -1)
    missing_sensor = "R00_SG0" if not amplifiers else "R04_SG0"
    with pytest.raises(KeyError, match=missing_sensor):
        OffsetCombiner(amplifiers).combine(0, samples, references)


@pytest.mark.parametrize("error_type", [LookupError, RuntimeError])
def test_transform_errors_propagate_without_raw_fallback(error_type):
    error = error_type("transform failed")

    class BrokenOrientation:
        def amplifier_to_camera_offset(self, *args):
            raise error

    with pytest.raises(error_type, match="transform failed") as exc:
        OffsetCombiner({"a": "C05"}, BrokenOrientation()).combine(
            0, {"a": measurement(1, 2)}, {"a": CentroidMeasurement(x=0, y=0)}
        )
    assert exc.value is error


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("target", ["measurement", "reference"])
@pytest.mark.parametrize("axis", ["x", "y"])
def test_invalid_contributing_coordinates_propagate(value, target, axis):
    sample = measurement(1, 2)
    reference = CentroidMeasurement(x=0, y=0)
    setattr(sample if target == "measurement" else reference, axis, value)
    with pytest.raises(ValueError, match="finite"):
        OffsetCombiner().combine(0, {"a": sample}, {"a": reference})


def test_tuple_reference_is_not_accepted():
    with pytest.raises(TypeError, match="CentroidMeasurement"):
        OffsetCombiner().combine(0, {"a": measurement(1, 2)}, {"a": (0, 0)})


@pytest.mark.parametrize(
    "segment", [None, "unknown", "Segment", "Segment5", "Segment005"]
)
def test_amplifier_map_rejects_missing_or_malformed_metadata(segment):
    sequence = GuiderSequence(np.zeros((1, 3, 4)), np.array([60000.0]), "a", segment)
    with pytest.raises(ValueError, match="Invalid ROISEG .* for sensor 'a'"):
        build_sensor_amplifiers({"a": sequence})
