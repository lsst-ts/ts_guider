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

__all__ = ["GuidingStatus"]

import enum


class GuidingStatus(enum.IntEnum):
    """Per-sensor tracking state, independent of DAQ series events.

    Measurement quality is reported separately on each centroid result.
    Automatic transitions to LOST are reserved for a future agreed loss
    and recovery policy; the current tracker does not emit that state.
    """

    # No stamps have been processed since construction or reset.
    NONE = enum.auto()
    # Accumulating seeds and trying to select an acceptable target.
    LOCKING = enum.auto()
    # A target is selected; individual measurements may still be rejected.
    LOCKED = enum.auto()
    # Tracking has been lost but is recoverable; transition policy pending.
    LOST = enum.auto()
    # Processing failed; explicit reset or reference locking is required.
    ERROR = enum.auto()
