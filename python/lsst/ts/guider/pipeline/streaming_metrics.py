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
    """Counts and timings accumulated over a streaming processor's lifetime.

    The processor keeps these counts and timing samples for the whole run,
    including across changes of DAQ series. All durations are measured in
    seconds using ``time.perf_counter``.

    Parameters
    ----------
    total_stamps : `int`, optional
        Number of ``on_stamp`` calls, including calls ignored after
        finalization or ending in error.
    seed_stamps : `int`, optional
        Number of stamps processed before their sensor has a reference,
        including the stamp that allows a reference to be chosen.
    valid_measurements : `int`, optional
        Number of returned measurements marked ``PASSED_QUALITY`` after
        passing the quality checks. This includes measurements of stored
        seed images after the reference star has been chosen.
    invalid_measurements : `int`, optional
        Number of returned measurements in any other state, including
        failed centroid fits and rejected measurements of stored seed images.
    measure_times : `list` [`float`], optional
        Time in seconds spent producing each returned centroid measurement,
        in the order the tracker returned the results. This includes results
        that failed quality checks. After choosing a reference, the tracker
        measures the stored seed images again; these times are included too.
        Time spent evaluating potential reference stars belongs to
        ``lock_times`` instead.
    lock_times : `list` [`float`], optional
        Time in seconds spent on each attempt to choose a reference star.
        This includes finding potential stars and measuring them in the seed
        images to decide which one meets the selection criteria. An attempt
        is recorded even if no suitable star is found.
    callback_times : `list` [`float`], optional
        Elapsed time for each ``on_stamp`` call, including ignored calls
        and calls ending in error.
    combine_times : `list` [`float`], optional
        Time in seconds spent in each completed ``OffsetCombiner.combine``
        call. The timer stops before ``on_combined_offset`` receives the
        result, so time spent handling that result is not included.
    first_stamp_time : `float` or `None`, optional
        Local ``time.perf_counter`` reading when the first ``on_stamp``
        call starts, used with ``last_stamp_time`` to compute elapsed time.
        `None` before any stamp arrives.
    last_stamp_time : `float` or `None`, optional
        Local ``time.perf_counter`` reading at the end of the latest timed
        ``on_stamp`` call, used with ``first_stamp_time`` to compute elapsed
        time.
        `None` before the first call completes.
    discarded_seed_measurements : `int`, optional
        Replayed results excluded from already-combined acquisitions.
        These remain included in the valid/invalid counts and measurement
        timings; this count is a subset of those results.
    """

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
        """Elapsed seconds from the first callback start to the latest end.

        Includes idle time between callbacks. Return zero until both
        clock readings are available.
        """
        if self.first_stamp_time is None or self.last_stamp_time is None:
            return 0.0
        return self.last_stamp_time - self.first_stamp_time
