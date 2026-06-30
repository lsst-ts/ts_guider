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

"""Combine per-sensor offsets into one camera-frame translation offset."""

from __future__ import annotations

__all__ = ["OffsetCombiner", "build_sensor_amplifiers"]

import logging

import numpy as np

from .. import sensor_orientation
from .models import CentroidMeasurement, CombinedOffset, GuiderSequence

log = logging.getLogger(__name__)


class OffsetCombiner:
    """Combine valid per-sensor offsets into one translation offset.

    Each per-sensor offset is ``(measured - reference)`` in amplifier
    (stamp) pixels. When a per-sensor amplifier map is supplied, every
    offset is first rotated into the common camera frame (amplifier
    flip + detector ``nQuarter``) via
    :func:`sensor_orientation.amplifier_to_camera_offset`, so the
    average is taken in a single orientation - the transform described
    at the 2026-04-22 meeting. Without that map the offsets are
    averaged in amplifier coordinates as before (only valid when all
    sensors already share an orientation).

    Parameters
    ----------
    sensor_amplifiers : `dict` [`str`, `str`], optional
        Map of sensor name (e.g. ``"R00_SG0"``) to the amplifier its
        ROI sits on (e.g. ``"C05"``). Sensors absent from the map, or
        an unset map, are left in amplifier coordinates.
    """

    def __init__(self, sensor_amplifiers: dict[str, str] | None = None):
        self.sensor_amplifiers = sensor_amplifiers or {}

    def combine(
        self,
        stamp_index: int,
        measurements: dict[str, CentroidMeasurement],
        references: dict[str, tuple[float, float]],
    ) -> CombinedOffset:
        per_sensor_offset: dict[str, tuple[float, float]] = {}
        for sensor_name, measurement in measurements.items():
            if not measurement.passed_quality:
                continue
            if sensor_name not in references:
                continue
            reference_x, reference_y = references[sensor_name]
            offset_x = measurement.x - reference_x
            offset_y = measurement.y - reference_y
            per_sensor_offset[sensor_name] = self._to_camera_frame(
                sensor_name, offset_x, offset_y
            )

        n_total = len(measurements)
        n_valid = len(per_sensor_offset)
        if n_valid == 0:
            return CombinedOffset(
                stamp_index=stamp_index,
                combined_dx=float("nan"),
                combined_dy=float("nan"),
                n_valid=0,
                n_total=n_total,
                scatter_dx=float("nan"),
                scatter_dy=float("nan"),
            )

        offsets = np.array(list(per_sensor_offset.values()))
        combined = offsets.mean(axis=0)
        if n_valid > 1:
            # Sample spread across sensors, and the uncertainty on the
            # average (standard error of the mean). The error is what
            # the pointing CSC should consume as the offset uncertainty;
            # the scatter reports how much the sensors disagree.
            scatter = offsets.std(axis=0, ddof=1)
            error = scatter / np.sqrt(n_valid)
        else:
            scatter = np.array([float("nan"), float("nan")])
            error = np.array([float("nan"), float("nan")])
        return CombinedOffset(
            stamp_index=stamp_index,
            combined_dx=float(combined[0]),
            combined_dy=float(combined[1]),
            n_valid=n_valid,
            n_total=n_total,
            scatter_dx=float(scatter[0]),
            scatter_dy=float(scatter[1]),
            error_dx=float(error[0]),
            error_dy=float(error[1]),
            per_sensor_offset=per_sensor_offset,
        )

    def _to_camera_frame(
        self, sensor_name: str, offset_x: float, offset_y: float
    ) -> tuple[float, float]:
        """Rotate one offset into the camera frame when possible.

        Falls back to the amplifier-frame offset if no amplifier is
        known for the sensor, or the sensor/amplifier is not in the
        orientation tables.
        """
        amplifier_name = self.sensor_amplifiers.get(sensor_name)
        if amplifier_name is None:
            return offset_x, offset_y
        try:
            return sensor_orientation.amplifier_to_camera_offset(
                offset_x, offset_y, sensor_name, amplifier_name
            )
        except KeyError:
            log.warning(
                "No orientation entry for sensor '%s' amplifier '%s'; "
                "leaving its offset in amplifier coordinates.",
                sensor_name,
                amplifier_name,
            )
            return offset_x, offset_y


def build_sensor_amplifiers(
    sequences: dict[str, GuiderSequence],
) -> dict[str, str]:
    """Map each sensor to its ROI amplifier from the ``ROISEG`` header.

    Returns an empty map when no sequence carries a usable ``Segment``
    value, in which case the combiner stays in amplifier coordinates.
    """
    sensor_amplifiers: dict[str, str] = {}
    for sensor_name, sequence in sequences.items():
        segment = sequence.segment
        if segment and segment.startswith("Segment"):
            sensor_amplifiers[sensor_name] = (
                sensor_orientation.amplifier_name_from_segment(segment)
            )
    return sensor_amplifiers
