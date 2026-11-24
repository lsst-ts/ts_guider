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
from unittest.mock import Mock

import numpy as np
import pytest
from lsst.ts.guider import guider_orientation
from lsst.ts.guider.guider_orientation import (
    GuiderOrientation,
    amplifier_name_from_segment,
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
def test_bundled_corner_orientations(sensor, expected):
    # C05 has raw X flip on these guiders. Their nQuarter values are
    # respectively 5, 6, 4, and 3, covering all four rotations modulo 4.
    orientation = GuiderOrientation()
    assert orientation.get_amplifier_flip(sensor, "C05") == (True, False)
    assert orientation.amplifier_to_camera_offset(2, -1, sensor, "C05") == expected


def test_orientation_table_contains_all_eight_guiders(capsys):
    orientation = GuiderOrientation()
    table = orientation.generate_orientation_table()
    expected = {
        f"{raft}_SG{sensor}"
        for raft in ("R00", "R04", "R40", "R44")
        for sensor in (0, 1)
    }
    assert set(table) == expected
    assert all("C05" in entry["amplifier_flip"] for entry in table.values())

    orientation.print_orientation_table()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:3] == ["detector", "detId", "nQuarter"]
    assert len(lines[2:]) == len(expected)
    assert {line.split()[0] for line in lines[2:]} == expected


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


def test_geometry_selection_without_camera_package(monkeypatch):
    missing = ModuleNotFoundError(name="lsst.obs")
    importer = Mock(side_effect=missing)
    monkeypatch.setattr(guider_orientation, "import_module", importer)
    assert GuiderOrientation().geometry_source == "local"
    assert GuiderOrientation(geometry_source="local").geometry_source == "local"
    importer.assert_not_called()
    orientation = GuiderOrientation(geometry_source="auto")
    assert orientation.geometry_source == "local"
    assert orientation.amplifier_to_camera_offset(2, -1, "R00_SG0", "C05") == (1, -2)
    with pytest.raises(ModuleNotFoundError, match="requires the obs_lsst"):
        GuiderOrientation(geometry_source="lsst")
    for sensor, amp in [("unknown", "C05"), ("R00_SG0", "C09")]:
        with pytest.raises(KeyError):
            orientation.get_amplifier_flip(sensor, amp)


def test_geometry_selection_with_camera_package(monkeypatch):
    model = Mock()
    importer = Mock(return_value=SimpleNamespace(LsstCam=model))
    monkeypatch.setattr(guider_orientation, "import_module", importer)
    for choice in ("auto", "lsst"):
        orientation = GuiderOrientation(geometry_source=choice)
        assert orientation.geometry_source == "lsst"
        assert orientation.camera is model.getCamera.return_value
    importer.side_effect = ModuleNotFoundError(name="lsst.afw")
    with pytest.raises(ModuleNotFoundError) as caught:
        GuiderOrientation(geometry_source="auto")
    assert caught.value is importer.side_effect
    importer.side_effect = None
    model.getCamera.side_effect = RuntimeError("invalid camera")
    with pytest.raises(RuntimeError, match="invalid camera"):
        GuiderOrientation(geometry_source="auto")


def test_bundled_table_matches_installed_camera():
    try:
        from lsst.obs.lsst import LsstCam
    except ModuleNotFoundError as error:
        if error.name in ("lsst.obs", "lsst.obs.lsst"):
            pytest.skip("obs_lsst is unavailable")
        raise
    local = GuiderOrientation()
    model = GuiderOrientation(LsstCam.getCamera())
    table = local.generate_orientation_table()
    assert len(table) == 8
    assert all(len(entry["amplifier_flip"]) == 16 for entry in table.values())
    assert table == model.generate_orientation_table()
    for sensor, entry in table.items():
        for amplifier in entry["amplifier_flip"]:
            for dx, dy in ((1, 0), (0, 1)):
                assert local.amplifier_to_camera_offset(dx, dy, sensor, amplifier) == (
                    model.amplifier_to_camera_offset(dx, dy, sensor, amplifier)
                )
