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

__all__ = ["MultiSensorRunner", "run_offline"]

import logging
from pathlib import Path

import numpy as np

from ..guider_orientation import GuiderOrientation
from .centroid_measurement import CentroidMeasurement
from .combined_offset import CombinedOffset
from .fits_io import read_guider_sequence
from .guider_tracker_config import GuiderTrackerConfig
from .offset_combiner import OffsetCombiner, build_sensor_amplifiers
from .sensor_tracker import SensorTracker

log = logging.getLogger(__name__)


class MultiSensorRunner:
    """Drive one SensorTracker per sensor and combine per acquisition.

    Direct callers can lock references from explicitly supplied seed
    cubes, then measure aligned acquisitions against those references.
    ``run_offline`` instead feeds these trackers through ``process_stamp``
    for automatic seed accumulation, locking retries, and seed replay.

    Parameters
    ----------
    sensor_names : `list` [`str`]
        Names of sensors to track.
    config : `GuiderTrackerConfig`
        Mutable configuration shared by all trackers and their detectors.
        Field updates affect subsequent processing without resetting
        existing references or recomputing previous results.
    sensor_amplifiers : `dict` [`str`, `str`], optional
        Sensor-to-amplifier map for camera-coordinate combination. Every
        contributing sensor requires an entry. With ``None``, offsets
        stay in amplifier coordinates and must already share an orientation.
    orientation : `GuiderOrientation`, optional
        Camera-model transform provider, optionally shared with other
        runners. The default builds its camera lazily on first use.
    """

    def __init__(
        self,
        sensor_names: list[str],
        config: GuiderTrackerConfig,
        sensor_amplifiers: dict[str, str] | None = None,
        orientation: GuiderOrientation | None = None,
    ):
        self.config = config
        self.trackers = {name: SensorTracker(name, config) for name in sensor_names}
        self.combiner = OffsetCombiner(sensor_amplifiers, orientation)

    def lock_references(
        self, seed_stamps_by_sensor: dict[str, np.ndarray]
    ) -> list[str]:
        """Replace references for the supplied subset of known sensors.

        Parameters
        ----------
        seed_stamps_by_sensor : `dict` [`str`, `numpy.ndarray`]
            Nonempty ``(frames, rows, columns)`` seed cubes. Each supplied
            known tracker is reset and makes one locking attempt using
            every frame supplied, independent of ``config.seed_frames``.
            Omitted trackers keep their previous state; unknown names
            are ignored. Use a new runner to start an independent run.

        Returns
        -------
        locked : `list` [`str`]
            Supplied sensors that locked on this call, in tracker order.
            A normal failed lock leaves that sensor without a reference.

        Raises
        ------
        ValueError
            If a supplied seed cube is malformed. Tracker processing
            errors propagate; updates to other trackers are not rolled back.
        """
        locked: list[str] = []
        for sensor_name, tracker in self.trackers.items():
            seed = seed_stamps_by_sensor.get(sensor_name)
            if seed is not None and tracker.lock_reference(seed):
                locked.append(sensor_name)
        return locked

    @property
    def references(self) -> dict[str, CentroidMeasurement]:
        """Available coordinate-only references, keyed by sensor name.

        The dictionary values are the trackers' reference objects.
        Each reference holds detected coordinates and with state
        NOT_SET.
        Note that Per-stamp HSM measurements are separate objects
        with their own states
        """
        return {
            name: tracker.reference
            for name, tracker in self.trackers.items()
            if tracker.reference is not None
        }

    def process_acquisition(
        self, stamp_index: int, stamps_by_sensor: dict[str, np.ndarray]
    ) -> tuple[dict[str, CentroidMeasurement], CombinedOffset]:
        """Measure delivered stamps and combine one aligned acquisition.

        Parameters
        ----------
        stamp_index : `int`
            Acquisition index passed through to the combined result.
        stamps_by_sensor : `dict` [`str`, `numpy.ndarray`]
            Two-dimensional stamps already aligned to this acquisition.
            Unknown sensors and sensors without references are skipped.

        Returns
        -------
        measurements : `dict` [`str`, `CentroidMeasurement`]
            Measurements for delivered, known sensors with references,
            including rejected measurements in amplifier coordinates.
        combined : `CombinedOffset`
            Mean of the accepted offsets. ``n_total`` counts measurements,
            not all configured sensors. Zero contributors produce NaNs.

        Raises
        ------
        ValueError
            If stamps, coordinates, or required camera metadata are
            invalid. Other tracker and transformation errors propagate.
        """
        measurements: dict[str, CentroidMeasurement] = {}
        for sensor_name, stamp in stamps_by_sensor.items():
            tracker = self.trackers.get(sensor_name)
            if tracker is None or tracker.reference is None:
                continue
            measurements[sensor_name] = tracker.measure(stamp)

        combined = self.combiner.combine(stamp_index, measurements, self.references)
        return measurements, combined


def run_offline(
    fits_paths: list[Path], config: GuiderTrackerConfig | None = None
) -> list[CombinedOffset]:
    """Run the full multi-sensor loop over aligned FITS sequences.

    Each file is one sensor. Stamps must already be aligned by array
    index across sensors; timestamps are retained but not used to align
    or validate acquisitions. All files are read into memory. Each tracker
    processes its images in acquisition order using ``process_stamp``;
    replayed seed measurements retain their original acquisition indices.
    Results are combined after processing so sensors that lock at different
    indices can contribute to the same earlier acquisitions.

    Parameters
    ----------
    fits_paths : `list` [`pathlib.Path`]
        One FITS sequence per distinct sensor, with valid sensor names
        and ``ROISEG=SegmentNN`` metadata for camera transformation.
    config : `GuiderTrackerConfig`, optional
        Shared tracker settings; defaults to a fresh configuration.
        The first reference-locking attempt uses ``seed_frames`` images.
        On failure, retain the seeds and retry with each additional image
        in the common input range, using the entire accumulated prefix.
        Once locked, the reference remains fixed for subsequent images.

    Returns
    -------
    combined_offsets : `list` [`CombinedOffset`]
        Camera-pixel results in acquisition order, including seed frames,
        ending at the shortest sequence even if its sensor did not lock.
        Images beyond that limit are not used, including for locking.
        Sensors without a reference at the end contribute no measurements.
        Empty if no inputs are supplied or no sensor locks, including when
        the common range is shorter than ``seed_frames``. There is no
        forced locking attempt on a shorter prefix at end of input.

    Raises
    ------
    ValueError
        If files identify the same sensor, contain invalid image cubes,
        or lack usable camera metadata. Reader, tracker, and unexpected
        camera errors propagate.
    """
    config = config if config is not None else GuiderTrackerConfig()

    sequences = {}
    sensor_paths: dict[str, Path] = {}
    for path in fits_paths:
        sequence = read_guider_sequence(path)
        sensor_name = sequence.sensor_name
        if sensor_name in sequences:
            raise ValueError(
                f"Duplicate sensor {sensor_name!r} in {str(sensor_paths[sensor_name])!r} "
                f"and {str(path)!r}. Supply one file per sensor."
            )
        sequences[sensor_name] = sequence
        sensor_paths[sensor_name] = path
    if not sequences:
        return []

    sensor_amplifiers = build_sensor_amplifiers(sequences)
    runner = MultiSensorRunner(list(sequences), config, sensor_amplifiers)
    n_acquisitions = min(seq.n_stamps for seq in sequences.values())
    measurements_by_acquisition: list[dict[str, CentroidMeasurement]] = [
        {} for _ in range(n_acquisitions)
    ]
    for sensor_name, sequence in sequences.items():
        tracker = runner.trackers[sensor_name]
        for stamp_index in range(n_acquisitions):
            tracker.process_stamp(sequence.stamps[stamp_index])
            measurements = tracker.get_measurements()
            # A successful lock replays the full prefix, including rejects.
            # Later calls return one measurement. The images are contiguous
            # from index zero, so either batch ends at this stamp's index.
            first_index = stamp_index + 1 - len(measurements)
            for acquisition_index, measurement in enumerate(
                measurements, start=first_index
            ):
                measurements_by_acquisition[acquisition_index][
                    sensor_name
                ] = measurement

    references = runner.references
    if not references:
        log.error(
            "No sensor locked a reference before the end of the common input range."
        )
        return []

    return [
        runner.combiner.combine(stamp_index, measurements, references)
        for stamp_index, measurements in enumerate(measurements_by_acquisition)
    ]
