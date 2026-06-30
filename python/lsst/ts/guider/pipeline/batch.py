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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Synchronous multi-sensor driver for aligned FITS sequences.

This is the offline counterpart to
:class:`~lsst.ts.guider.pipeline.streaming.StreamingGuiderProcessor`:
it has every stamp in hand, so it locks all references up front and
loops over acquisitions in index order. Both drivers share the same
:class:`SensorTracker` and :class:`OffsetCombiner`.
"""

from __future__ import annotations

__all__ = ["MultiSensorRunner", "run_offline"]

import logging
from pathlib import Path

import numpy as np

from .fits_io import read_guider_sequence
from .models import CentroidMeasurement, CombinedOffset, GuiderTrackerConfig
from .offset_combiner import OffsetCombiner, build_sensor_amplifiers
from .sensor_tracker import SensorTracker

log = logging.getLogger(__name__)


class MultiSensorRunner:
    """Drive one SensorTracker per sensor and combine per acquisition."""

    def __init__(
        self,
        sensor_names: list[str],
        config: GuiderTrackerConfig,
        sensor_amplifiers: dict[str, str] | None = None,
    ):
        self.config = config
        self.trackers = {name: SensorTracker(name, config) for name in sensor_names}
        self.combiner = OffsetCombiner(sensor_amplifiers)

    def lock_references(
        self, seed_stamps_by_sensor: dict[str, np.ndarray]
    ) -> list[str]:
        """Lock each sensor's reference. Returns the sensors that locked."""
        locked: list[str] = []
        for sensor_name, tracker in self.trackers.items():
            seed = seed_stamps_by_sensor.get(sensor_name)
            if seed is not None and tracker.lock_reference(seed):
                locked.append(sensor_name)
        return locked

    @property
    def references(self) -> dict[str, tuple[float, float]]:
        return {
            name: tracker.reference_center
            for name, tracker in self.trackers.items()
            if tracker.reference_center is not None
        }

    def process_acquisition(
        self, stamp_index: int, stamps_by_sensor: dict[str, np.ndarray]
    ) -> tuple[dict[str, CentroidMeasurement], CombinedOffset]:
        """Measure all delivered sensors and combine into one offset."""
        measurements: dict[str, CentroidMeasurement] = {}
        for sensor_name, stamp in stamps_by_sensor.items():
            tracker = self.trackers.get(sensor_name)
            if tracker is None or tracker.reference_center is None:
                continue
            measurements[sensor_name] = tracker.measure(stamp)

        combined = self.combiner.combine(stamp_index, measurements, self.references)
        return measurements, combined


def run_offline(
    fits_paths: list[Path], config: GuiderTrackerConfig | None = None
) -> list[CombinedOffset]:
    """Run the full multi-sensor loop over aligned FITS sequences.

    Each file is one sensor. Stamps are assumed aligned by index across
    sensors (verified for the emulator data); production code keyed on
    the DAQ ``stamp_index`` would group asynchronously delivered stamps
    instead.
    """
    config = config if config is not None else GuiderTrackerConfig()

    sequences = {
        seq.sensor_name: seq
        for seq in (read_guider_sequence(path) for path in fits_paths)
    }
    sensor_amplifiers = build_sensor_amplifiers(sequences)
    runner = MultiSensorRunner(list(sequences), config, sensor_amplifiers)
    locked = runner.lock_references(
        {name: seq.stamps for name, seq in sequences.items()}
    )
    if not locked:
        log.error("No sensor locked a reference; aborting.")
        return []

    n_acquisitions = min(seq.n_stamps for seq in sequences.values())
    combined_offsets: list[CombinedOffset] = []
    for stamp_index in range(n_acquisitions):
        stamps_by_sensor = {
            name: seq.stamps[stamp_index] for name, seq in sequences.items()
        }
        _, combined = runner.process_acquisition(stamp_index, stamps_by_sensor)
        combined_offsets.append(combined)

    return combined_offsets
