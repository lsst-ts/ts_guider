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

from lsst.ts.guider.pipeline import CentroidMeasurement


def test_signed_local_offset_retains_quality_information():
    measurement = CentroidMeasurement(
        x=102.25, y=79.5, converged=True, passed_quality=False
    )
    assert measurement.offset_from((100, 80)) == (2.25, -0.5)
    assert measurement.is_finite
    assert not measurement.passed_quality


def test_failed_measurement_has_no_usable_offset():
    measurement = CentroidMeasurement()
    assert not measurement.converged
    assert not measurement.passed_quality
    assert not measurement.is_finite
    assert all(math.isnan(value) for value in measurement.offset_from((100, 80)))
