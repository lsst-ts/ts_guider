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

import pytest
from lsst.ts.guider.pipeline import CentroidMeasurement, MeasurementState


def test_signed_local_offset_retains_quality_information():
    measurement = CentroidMeasurement(
        x=102.25, y=79.5, state=MeasurementState.CONVERGED
    )
    reference = CentroidMeasurement(x=100, y=80)
    assert measurement.offset_from(reference) == (2.25, -0.5)
    assert measurement.is_finite
    assert measurement.state == MeasurementState.CONVERGED


@pytest.mark.parametrize(
    "state", [MeasurementState.NOT_SET, MeasurementState.NOT_CONVERGED]
)
def test_unmeasured_or_failed_result_has_no_usable_offset(state):
    measurement = CentroidMeasurement(state=state)
    assert not measurement.is_finite
    with pytest.raises(ValueError, match="non-finite"):
        measurement.offset_from(CentroidMeasurement(x=100, y=80))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("coordinate", ["x", "y"])
@pytest.mark.parametrize("invalid_reference", [False, True])
def test_nonfinite_coordinate_in_either_centroid(value, coordinate, invalid_reference):
    measurement = CentroidMeasurement(x=102, y=79)
    reference = CentroidMeasurement(x=100, y=80)
    invalid = reference if invalid_reference else measurement
    setattr(invalid, coordinate, value)
    with pytest.raises(ValueError, match="non-finite"):
        measurement.offset_from(reference)


@pytest.mark.parametrize("reference", [(100, 80), [100, 80], None, 1])
def test_wrong_reference_type(reference):
    with pytest.raises(TypeError, match="CentroidMeasurement"):
        CentroidMeasurement(x=102, y=79).offset_from(reference)


def test_default_reference_has_no_usable_coordinates():
    reference = CentroidMeasurement()
    assert reference.state == MeasurementState.NOT_SET
    with pytest.raises(ValueError, match="non-finite"):
        CentroidMeasurement(x=102, y=79).offset_from(reference)


@pytest.mark.parametrize("dx,dy", [(2, -1), (-2.5, 3.25), (0, 0)])
def test_offset_sign_and_axes(dx, dy):
    reference = CentroidMeasurement(x=100, y=80)
    measurement = CentroidMeasurement(x=100 + dx, y=80 + dy)
    assert measurement.offset_from(reference) == (dx, dy)
