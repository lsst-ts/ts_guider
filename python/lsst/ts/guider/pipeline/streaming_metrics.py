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

from __future__ import annotations

__all__ = ["StreamingMetrics"]

from dataclasses import dataclass, field


@dataclass
class StreamingMetrics:
    """Timing and counts accumulated while the stream is running."""

    total_stamps: int = 0
    seed_stamps: int = 0
    valid_measurements: int = 0
    invalid_measurements: int = 0
    measure_times: list[float] = field(default_factory=list)
    lock_times: list[float] = field(default_factory=list)
    callback_times: list[float] = field(default_factory=list)
    combine_times: list[float] = field(default_factory=list)
    first_stamp_time: float | None = None
    last_stamp_time: float | None = None
    discarded_seed_measurements: int = 0

    @property
    def stream_seconds(self) -> float:
        if self.first_stamp_time is None or self.last_stamp_time is None:
            return 0.0
        return self.last_stamp_time - self.first_stamp_time
