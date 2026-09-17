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
from lsst.ts.guider.sensor_identifiers import (
    amplifier_name_from_segment_index,
    detector_name_from_sensor_index,
)


@pytest.mark.parametrize(
    "index,name",
    [
        (3, "R00_SG0"),
        (4, "R00_SG1"),
        (39, "R04_SG0"),
        (40, "R04_SG1"),
        (183, "R40_SG0"),
        (184, "R40_SG1"),
        (219, "R44_SG0"),
        (220, "R44_SG1"),
    ],
)
def test_guide_sensor_indices(index, name):
    assert detector_name_from_sensor_index(index) == name


@pytest.mark.parametrize("index", [0, 6, 180, 186])
def test_other_boards_are_rejected(index):
    with pytest.raises(ValueError, match="not a guide sensor"):
        detector_name_from_sensor_index(index)


@pytest.mark.parametrize(
    "segment,name", [(0, "C00"), (5, "C05"), (10, "C10"), (17, "C17")]
)
def test_daq_segment_numbering(segment, name):
    assert amplifier_name_from_segment_index(segment) == name
