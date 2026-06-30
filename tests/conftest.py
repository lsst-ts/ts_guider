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

"""Build the C++-backed guiderGDS module before collection.

The binding tests in test_guiderGDS_bindings.py import guiderGDS,
which has to be compiled first. This runs before collection so the
importorskip in those tests can succeed:

  * If guiderGDS is already importable (a developer put a build dir on
    PYTHONPATH), do nothing.
  * Else if the daq-sdk is available (CCSDAQ set), build the module and
    add the build directory to sys.path.
  * Else do nothing; the binding tests skip via importorskip.

Note: importing the built module still needs the daq-sdk shared
libraries (and their soname shims) on LD_LIBRARY_PATH. That is the
environment/image's responsibility and cannot be fixed here, since the
dynamic linker reads LD_LIBRARY_PATH at process start. If the import
fails for that reason the binding tests simply skip.
"""

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile

CPP_CMAKE_DIR = pathlib.Path(__file__).parents[1] / "cpp" / "cmake"


def pytest_configure(config):
    if importlib.util.find_spec("guiderGDS") is not None:
        return

    if not os.environ.get("CCSDAQ"):
        return

    build_dir = tempfile.mkdtemp(prefix="guiderGDS_build_")
    try:
        subprocess.run(
            [
                "cmake",
                "-S",
                str(CPP_CMAKE_DIR),
                "-B",
                build_dir,
                "-DBUILD_MODULE=ON",
                "-DBUILD_TESTS=OFF",
            ],
            check=True,
        )
        subprocess.run(["cmake", "--build", build_dir], check=True)
    except (OSError, subprocess.CalledProcessError):
        return

    sys.path.insert(0, build_dir)
