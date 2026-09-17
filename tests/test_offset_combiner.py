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
    OffsetCombiner,
    build_sensor_amplifiers,
)


def measurement(x, y, *, accepted=True):
    return CentroidMeasurement(
        x=x, y=y, snr=100, fwhm=4, e1=0, e2=0, converged=True, passed_quality=accepted
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
    references = {"R00_SG0": (100, 80), "R04_SG0": (200, 120), "rejected": (0, 0)}
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
    assert not result.measurements["rejected"].passed_quality


def test_one_valid_sensor_has_no_scatter_estimate():
    result = OffsetCombiner({"R00_SG0": "C05"}).combine(
        5, {"R00_SG0": measurement(11, 18)}, {"R00_SG0": (10, 20)}
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
        0, measurements, {"R00_SG0": (100, 80)}
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
        {"a": (10, 20), "b": (20, 30)},
    )
    assert (result.combined_dx, result.combined_dy) == (3, 0)


def test_build_amplifier_map_uses_fits_segment():
    stamps = np.zeros((1, 60, 60))
    sequences = {
        name: GuiderSequence(stamps, np.array([60000.0]), name, segment)
        for name, segment in [("a", "Segment05"), ("b", None), ("c", "unknown")]
    }
    assert build_sensor_amplifiers(sequences) == {"a": "C05"}
