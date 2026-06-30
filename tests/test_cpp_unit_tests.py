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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Run the SDK-free C++ GoogleTest suite from within pytest.

The DevelopPipeline CI stage only invokes pytest, so it would never
see a standalone C++ test suite. This wraps the cmake/ctest build of
the C++ unit tests (cpp/cmake/tests, configured with BUILD_MODULE=OFF
so no daq-sdk is required) in a single pytest case. The ctest output
is surfaced in the normal CI report when something fails.
"""

import pathlib
import shutil
import subprocess

import pytest

CPP_CMAKE_DIR = pathlib.Path(__file__).parents[1] / "cpp" / "cmake"


def _run(command, **kwargs):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )
    if result.returncode != 0:
        pytest.fail(
            f"command failed ({result.returncode}): "
            f"{' '.join(command)}\n{result.stdout}",
            pytrace=False,
        )
    return result.stdout


@pytest.mark.skipif(shutil.which("cmake") is None, reason="cmake not available")
def test_cpp_unit_tests(tmp_path):
    build_dir = tmp_path / "build"
    _run(
        [
            "cmake",
            "-S",
            str(CPP_CMAKE_DIR),
            "-B",
            str(build_dir),
            "-DBUILD_MODULE=OFF",
            "-DBUILD_TESTS=ON",
        ]
    )
    _run(["cmake", "--build", str(build_dir)])
    _run(["ctest", "--test-dir", str(build_dir), "--output-on-failure"])
