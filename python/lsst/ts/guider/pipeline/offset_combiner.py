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

__all__ = ["OffsetCombiner", "build_sensor_amplifiers"]

import numpy as np

from .. import guider_orientation
from .centroid_measurement import CentroidMeasurement
from .combined_offset import CombinedOffset
from .guider_sequence import GuiderSequence
from .measurement_state import MeasurementState


class OffsetCombiner:
    """Combine valid per-sensor offsets into one translation offset.

    Each per-sensor offset is ``(measured - reference)`` in amplifier
    (stamp) pixels. When a per-sensor amplifier map is supplied, every
    offset is first rotated into the common camera frame (amplifier
    flip + detector ``nQuarter``) via
    :meth:`guider_orientation.GuiderOrientation.amplifier_to_camera_offset`,
    so the average is taken in a single orientation - the transform
    described at the 2026-04-22 meeting. Without that map the offsets
    are averaged in amplifier coordinates (only valid when all sensors
    already share an orientation).

    Parameters
    ----------
    sensor_amplifiers : `dict` [`str`, `str`], optional
        Map of sensor name (e.g. ``"R00_SG0"``) to the amplifier its
        ROI sits on (e.g. ``"C05"``). Supplying a map, even an empty one,
        selects camera coordinates and requires a valid entry for every
        contributing sensor. ``None`` selects amplifier coordinates;
        the caller must ensure those offsets already share an orientation.
        Valid guider metadata is assumed to identify sensors and
        amplifiers present in the camera model.
    orientation : `guider_orientation.GuiderOrientation`, optional
        Source of the per-sensor flip and rotation, read from the LSST
        camera model. Defaults to a fresh :class:`GuiderOrientation`
        (which builds the camera lazily on the first camera-frame
        transform). Pass a shared instance to reuse one camera across
        drivers.
    """

    def __init__(
        self,
        sensor_amplifiers: dict[str, str] | None = None,
        orientation: guider_orientation.GuiderOrientation | None = None,
    ):
        self.sensor_amplifiers = sensor_amplifiers
        self.orientation = orientation or guider_orientation.GuiderOrientation()

    def combine(
        self,
        stamp_index: int,
        measurements: dict[str, CentroidMeasurement],
        references: dict[str, CentroidMeasurement],
    ) -> CombinedOffset:
        """Average accepted offsets from one acquisition in a common frame.

        Parameters
        ----------
        stamp_index : `int`
            Acquisition index retained in the result.
        measurements : `dict` [`str`, `CentroidMeasurement`]
            Per-sensor measurements in zero-based amplifier pixels.
            Only ``PASSED_QUALITY`` measurements with a reference
            contribute. All supplied measurements remain in diagnostics.
        references : `dict` [`str`, `CentroidMeasurement`]
            Fixed per-sensor reference coordinates in the same local
            frame. Coordinate-only references with ``NOT_SET`` state
            are valid; reference quality is not tested here.

        Returns
        -------
        result : `CombinedOffset`
            Unweighted mean and sample scatter in pixels, with standard
            error ``scatter / sqrt(n_valid)``. Scatter and error are NaN
            for fewer than two contributors; the mean is NaN for none.

        Raises
        ------
        TypeError
            If a contributing reference is not a centroid measurement.
        ValueError
            If contributing coordinates are non-finite.
        KeyError
            If a contributing sensor is absent from the amplifier map.

        Notes
        -----
        Camera geometry is read directly for the supplied metadata.
        Lookup and processing exceptions propagate unchanged; no offset
        falls back from camera to amplifier coordinates.
        """
        per_sensor_offset: dict[str, tuple[float, float]] = {}
        for sensor_name, measurement in measurements.items():
            if measurement.state != MeasurementState.PASSED_QUALITY:
                continue
            if sensor_name not in references:
                continue
            offset_x, offset_y = measurement.offset_from(references[sensor_name])
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
                measurements=dict(measurements),
            )

        offsets = np.array(list(per_sensor_offset.values()))
        combined = offsets.mean(axis=0)
        if n_valid > 1:
            # Sample spread across sensors, and the uncertainty on the
            # average (standard error of the mean). This estimates the
            # uncertainty from sensor disagreement, without propagating
            # centroid-fit errors or common systematic errors.
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
            measurements=dict(measurements),
        )

    def _to_camera_frame(
        self, sensor_name: str, offset_x: float, offset_y: float
    ) -> tuple[float, float]:
        """Transform one offset using the metadata's camera-model entry."""
        if self.sensor_amplifiers is None:
            return offset_x, offset_y

        amplifier_name = self.sensor_amplifiers[sensor_name]
        return self.orientation.amplifier_to_camera_offset(
            offset_x, offset_y, sensor_name, amplifier_name
        )


def build_sensor_amplifiers(
    sequences: dict[str, GuiderSequence],
) -> dict[str, str]:
    """Map each sensor to its ROI amplifier from the ``ROISEG`` header.

    Parameters
    ----------
    sequences : `dict` [`str`, `GuiderSequence`]
        Sequences keyed by sensor name. Each must carry ``ROISEG`` in
        the form ``SegmentNN``, where NN is two ASCII digits.

    Returns
    -------
    sensor_amplifiers : `dict` [`str`, `str`]
        Complete sensor-to-amplifier map identifying each ROI's amplifier.
        Its orientation is read from the camera model during combination.

    Raises
    ------
    ValueError
        If a sequence has missing or malformed segment metadata.
    """
    sensor_amplifiers: dict[str, str] = {}
    for sensor_name, sequence in sequences.items():
        segment = sequence.segment
        try:
            sensor_amplifiers[sensor_name] = (
                guider_orientation.amplifier_name_from_segment(segment)
            )
        except ValueError as error:
            raise ValueError(
                f"Invalid ROISEG {segment!r} for sensor {sensor_name!r}."
            ) from error
    return sensor_amplifiers
