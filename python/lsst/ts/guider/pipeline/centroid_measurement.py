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

__all__ = ["CentroidMeasurement"]

from dataclasses import dataclass

import numpy as np


@dataclass
class CentroidMeasurement:
    """HSM centroid and shape for one stamp, in full-stamp pixels.

    Parameters
    ----------
    x, y : `float`
        Zero-based centroid coordinates in the full input stamp, with
        x along columns and y along rows, in amplifier pixels.
    snr : `float`
        Aperture signal-to-noise ratio.
    fwhm : `float`
        Gaussian-equivalent width, ``2.355 * moments_sigma``, in pixels.
    e1, e2 : `float`
        GalSim adaptive-moment distortion components.
    converged : `bool`
        Whether the adaptive-moment fit reported success.
    passed_quality : `bool`
        Whether the measurement passed all configured quality cuts.

    Notes
    -----
    Failed measurements default to non-finite coordinates and false flags.
    A successful fit can still fail the quality cuts; retain its values
    for diagnostics and check ``passed_quality`` before using an offset.
    """

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
        """Whether both centroid coordinates are finite."""
        return bool(np.isfinite(self.x) and np.isfinite(self.y))

    def offset_from(self, reference_center: tuple[float, float]) -> tuple[float, float]:
        """Return ``measured - reference`` in amplifier pixel coordinates.

        Parameters
        ----------
        reference_center : `tuple` [`float`, `float`]
            Fixed ``(x, y)`` reference in the same stamp coordinates.

        Returns
        -------
        dx, dy : `float`
            Displacement along stamp columns and rows, respectively.
            No camera rotation or quality filtering is applied. Non-finite
            coordinates propagate to the corresponding displacement.
        """
        reference_x, reference_y = reference_center
        return float(self.x - reference_x), float(self.y - reference_y)
