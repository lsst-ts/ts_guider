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

__all__ = ["MeasurementState"]

import enum


class MeasurementState(enum.IntEnum):
    """Fit convergence and quality for one centroid measurement.

    This state describes an individual result, independently of the
    sensor's tracking state. A coordinate-only reference remains NOT_SET.
    """

    # No measurement outcome is available.
    NOT_SET = 0
    # The cutout was unusable or the adaptive-moment fit did not converge.
    NOT_CONVERGED = enum.auto()
    # The fit converged but the result did not pass the quality cuts.
    CONVERGED = enum.auto()
    # The fit converged and the result passed all quality cuts.
    PASSED_QUALITY = enum.auto()
