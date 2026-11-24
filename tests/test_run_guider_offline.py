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

import sys

import numpy as np
import pytest
from astropy.io import fits
from lsst.ts.guider.demo.run_guider_offline import run_guider_offline


@pytest.fixture
def guider_files(tmp_path):
    """Write two compressed sensor sequences with known camera motion."""
    offsets = [(0, 0)] * 3 + [(2, -1), (0.4, -0.65)]
    rows, columns = np.indices((100, 140))
    paths = []
    for sensor in ("R00_SG0", "R04_SG0"):
        primary = fits.PrimaryHDU()
        primary.header["RAFTBAY"], primary.header["CCDSLOT"] = sensor.split("_")
        primary.header["ROISEG"] = "Segment05"
        hdus = [primary]
        for index, (dx, dy) in enumerate(offsets):
            # Inverse of the C05 transforms, specified independently of
            # GuiderOrientation, as in the offline runner's numerical tests.
            local_x, local_y = (-dy, -dx) if sensor == "R00_SG0" else (dx, -dy)
            star = (
                100_000
                / (2 * np.pi * 2**2)
                * np.exp(
                    -((columns - 70.25 - local_x) ** 2 + (rows - 45.7 - local_y) ** 2)
                    / 8
                )
            )
            noise = np.random.default_rng(index).normal(0, 5, rows.shape)
            pixels = np.rint(100 + star + noise).astype(np.int32)
            hdus.append(fits.CompImageHDU(pixels, compression_type="RICE_1"))
        path = tmp_path / f"{sensor}.fits"
        fits.HDUList(hdus).writeto(path)
        paths.append(str(path))
    return paths, offsets


def test_command_prints_camera_offsets(guider_files, capsys):
    paths, offsets = guider_files
    status = run_guider_offline(paths + ["--seed-frames", "3", "--min-snr", "20"])
    assert status == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "stamp",
        "dx_px",
        "dy_px",
        "error_x_px",
        "error_y_px",
        "scatter_x_px",
        "scatter_y_px",
        "n_valid",
        "n_total",
    ]
    results = np.array([line.split() for line in lines[1:]], dtype=float)
    np.testing.assert_array_equal(results[:, 0], np.arange(len(offsets)))
    np.testing.assert_allclose(results[:, 1:3], offsets, atol=0.05)
    assert np.all(np.isfinite(results[:, 3:7]))
    np.testing.assert_allclose(results[:, 3:5], results[:, 5:7] / np.sqrt(2), atol=1e-6)
    assert np.all(results[:, 7:] == 2)


def test_command_no_reference_is_failure(guider_files, capsys, caplog):
    paths, _ = guider_files
    assert run_guider_offline(paths + ["--seed-frames", "3", "--min-snr", "1e12"]) == 1
    assert capsys.readouterr().out == ""
    assert "No combined offsets were produced" in caplog.text


def test_command_missing_file_is_failure(tmp_path, capsys, caplog):
    missing = tmp_path / "missing.fits"
    assert run_guider_offline([str(missing)]) == 1
    assert capsys.readouterr().out == ""
    assert "Offline processing failed" in caplog.text
    assert str(missing) in caplog.text


def test_command_duplicate_sensor_is_failure(guider_files, capsys, caplog):
    paths, _ = guider_files
    assert run_guider_offline([paths[0], paths[0]]) == 1
    assert capsys.readouterr().out == ""
    assert "Duplicate sensor" in caplog.text


@pytest.mark.parametrize(
    "options",
    [
        ["--seed-frames", "0"],
        ["--seed-frames", "-1"],
        ["--seed-frames", "1.5"],
        ["--min-snr", "0"],
        ["--min-snr", "-1"],
        ["--min-snr", "nan"],
        ["--min-snr", "inf"],
        ["--log-level", "unknown"],
        ["--geometry-source", "unknown"],
    ],
)
def test_command_rejects_invalid_options(options, capsys):
    with pytest.raises(SystemExit) as exc:
        run_guider_offline(["unused.fits"] + options)
    assert exc.value.code == 2
    assert options[0] in capsys.readouterr().err


def test_command_requires_input_files(capsys):
    with pytest.raises(SystemExit) as exc:
        run_guider_offline([])
    assert exc.value.code == 2
    assert "FITS" in capsys.readouterr().err


def test_command_help_without_daq(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "guiderGDS", None)
    with pytest.raises(SystemExit) as exc:
        run_guider_offline(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "run_guider_offline" in output
    assert "--seed-frames" in output
    assert "--min-snr" in output


def test_command_auto_without_camera(guider_files, monkeypatch, capsys, caplog):
    monkeypatch.setitem(sys.modules, "lsst.obs.lsst", None)
    paths, _ = guider_files
    with caplog.at_level("INFO"):
        assert (
            run_guider_offline(
                paths + ["--geometry-source", "auto", "--seed-frames", "3"]
            )
            == 0
        )
    assert "Guider geometry source: local" in caplog.text
    assert capsys.readouterr().out.splitlines()[1:]
    assert run_guider_offline(paths + ["--geometry-source", "lsst"]) == 1
    assert "requires the obs_lsst" in caplog.text
