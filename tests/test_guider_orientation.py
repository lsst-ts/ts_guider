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

from types import SimpleNamespace

import numpy as np
import pytest
from lsst.ts.guider.guider_orientation import (
    GuiderOrientation,
    amplifier_name_from_segment,
    generate_orientation_table,
)


@pytest.mark.parametrize("nquarter", range(4))
@pytest.mark.parametrize(
    "flip_x, flip_y", [(False, False), (True, False), (False, True), (True, True)]
)
def test_offset_matches_displacement_in_transformed_image(nquarter, flip_x, flip_y):
    # Two labeled pixels provide an independent image-space displacement;
    # this checks the vector transform against flips/rot90 without repeating
    # the matrix arithmetic being tested.
    amplifier = SimpleNamespace(getRawFlipX=lambda: flip_x, getRawFlipY=lambda: flip_y)
    detector = {"C05": amplifier}

    class Detector(dict):
        def getOrientation(self):
            return SimpleNamespace(getNQuarter=lambda: nquarter)

    orientation = GuiderOrientation({"test": Detector(detector)})
    image = np.zeros((7, 9))
    image[2, 2] = 1
    image[4, 5] = 2
    before = image.copy()
    transformed = orientation.amplifier_to_camera_view(image, "test", "C05")
    reference = np.argwhere(transformed == 1)[0][::-1]
    measured = np.argwhere(transformed == 2)[0][::-1]
    np.testing.assert_array_equal(
        orientation.amplifier_to_camera_offset(3, 2, "test", "C05"),
        measured - reference,
    )
    np.testing.assert_array_equal(image, before)


@pytest.mark.parametrize(
    "sensor, expected",
    [
        ("R00_SG0", (1, -2)),
        ("R04_SG0", (2, 1)),
        ("R40_SG0", (-2, -1)),
        ("R44_SG0", (-1, 2)),
    ],
)
def test_real_camera_corner_orientations(sensor, expected):
    # C05 has raw X flip on these guiders. Their nQuarter values are
    # respectively 5, 6, 4, and 3, covering all four rotations modulo 4.
    orientation = GuiderOrientation()
    assert orientation.get_amplifier_flip(sensor, "C05") == (True, False)
    assert orientation.amplifier_to_camera_offset(2, -1, sensor, "C05") == expected


def test_orientation_table_contains_all_eight_guiders():
    table = generate_orientation_table()
    expected = {
        f"{raft}_SG{sensor}"
        for raft in ("R00", "R04", "R40", "R44")
        for sensor in (0, 1)
    }
    assert set(table) == expected
    assert all("C05" in entry["amplifier_flip"] for entry in table.values())


@pytest.mark.parametrize(
    "segment, expected",
    [("Segment00", "C00"), ("Segment05", "C05"), ("Segment17", "C17")],
)
def test_fits_segment_to_amplifier(segment, expected):
    assert amplifier_name_from_segment(segment) == expected


@pytest.mark.parametrize(
    "segment",
    [
        None,
        5,
        "",
        "C05",
        "Segment",
        "Segment5",
        "Segment005",
        "Segment0x",
        "Segment05 ",
    ],
)
def test_malformed_segment_is_rejected(segment):
    with pytest.raises(ValueError, match="Expected ROISEG"):
        amplifier_name_from_segment(segment)
