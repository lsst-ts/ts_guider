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
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

try:
    from .version import __version__
except ImportError:
    __version__ = "?"

from .config_schema import *


def __getattr__(name):
    # Expose the salobj-based CSC lazily so that importing the tracking
    # algorithm (``lsst.ts.guider.pipeline`` / ``sensor_orientation``)
    # does not require salobj. ``guider.GuiderCsc`` and the
    # ``run_guider_csc`` entry point keep working via this hook.
    if name in ("GuiderCsc", "run_guider_csc"):
        from . import guider_csc

        return getattr(guider_csc, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
