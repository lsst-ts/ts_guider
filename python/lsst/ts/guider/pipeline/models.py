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

"""Configuration and result data structures for the guider pipeline."""

from __future__ import annotations

__all__ = [
    "GuiderTrackerConfig",
    "GuiderSequence",
    "CentroidMeasurement",
    "CombinedOffset",
]

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class GuiderTrackerConfig:
    """Algorithm and quality-cut configuration."""

    seed_frames: int = 30
    cutout_size: int = 50
    aperture_radius: float = 10.0
    gain: float = 1.0
    detection_threshold: float = 10.0
    edge_margin: int = 5
    n_pix_min: int = 10
    min_snr: float = 10.0
    max_ellipticity: float = 0.7
    # TBD: is this a good idea?
    # TBD: Make it configurable?
    # Reject sources wider than a star (blends, galaxies, hot columns)
    # even when they are bright enough to pass the SNR cut. Guide-star
    # FWHM is a few pixels; a lock at tens of pixels is not a star.
    max_fwhm: float = 15.0
    min_valid_stamp_fraction: float = 0.5


@dataclass
class GuiderSequence:
    """All stamps of one guide sequence for a single sensor."""

    stamps: np.ndarray
    timestamps_mjd: np.ndarray
    sensor_name: str
    segment: str | None = None

    @property
    def n_stamps(self) -> int:
        return self.stamps.shape[0]


@dataclass
class CentroidMeasurement:
    """HSM centroid and shape for one stamp, in full-stamp pixels."""

    x: float = float("nan")
    y: float = float("nan")
    snr: float = 0.0
    fwhm: float = float("nan")
    e1: float = float("nan")
    e2: float = float("nan")
    converged: bool = False
    passed_quality: bool = False

    @property
    def is_finite(self) -> bool:
        return bool(np.isfinite(self.x) and np.isfinite(self.y))


@dataclass
class CombinedOffset:
    """Combined translation offset for one acquisition."""

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
