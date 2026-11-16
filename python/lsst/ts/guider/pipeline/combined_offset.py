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

__all__ = ["CombinedOffset"]

from dataclasses import dataclass, field

from .centroid_measurement import CentroidMeasurement


@dataclass
class CombinedOffset:
    """Combined translation offset for one acquisition.

    Offsets, sample scatter, and standard errors are in pixels.
    ``n_total`` counts the supplied measurements; ``n_valid`` counts the
    offsets included in the mean. ``per_sensor_offset`` holds those
    offsets after the combiner's coordinate transform.

    ``measurements`` retains all supplied centroid measurements, including
    rejected measurements, for diagnostics and later publication.

    Parameters
    ----------
    stamp_index : `int`
        Acquisition index supplied by the caller.
    combined_dx, combined_dy : `float`
        Unweighted mean displacement in camera pixels when an amplifier
        map is supplied, otherwise in already-aligned amplifier pixels.
        NaN when no offsets contribute.
    n_valid, n_total : `int`
        Number of contributing offsets and supplied measurements,
        respectively. Sensors without measurements are not counted.
    scatter_dx, scatter_dy : `float`
        Per-axis sample standard deviation (``ddof=1``) in pixels.
        NaN for fewer than two contributors.
    error_dx, error_dy : `float`, optional
        Scatter divided by ``sqrt(n_valid)``, in pixels; NaN for fewer
        than two contributors. This describes sensor disagreement,
        without propagating fit errors or common systematic errors.
    per_sensor_offset : `dict` [`str`, `tuple` [`float`, `float`]], optional
        Included ``(dx, dy)`` offsets in the same frame as the mean.
    measurements : `dict` [`str`, `CentroidMeasurement`], optional
        All input measurements, including rejects. Their centroid X/Y
        coordinates remain in zero-based local stamp pixels (columns/rows).
    """

    stamp_index: int
    combined_dx: float
    combined_dy: float
    n_valid: int
    n_total: int
    scatter_dx: float
    scatter_dy: float
    error_dx: float = float("nan")
    error_dy: float = float("nan")
    per_sensor_offset: dict[str, tuple[float, float]] = field(default_factory=dict)
    measurements: dict[str, CentroidMeasurement] = field(default_factory=dict)
