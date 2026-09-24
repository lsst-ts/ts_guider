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

from .measurement_state import MeasurementState


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
    state : `MeasurementState`
        Fit convergence and quality outcome. Defaults to NOT_SET for an
        unmeasured result or a coordinate-only reference.

    Notes
    -----
    Failed measurement attempts have state NOT_CONVERGED and non-finite
    coordinates. A successful fit can still fail the quality cuts; its
    state is CONVERGED and its values are retained for diagnostics.
    Require ``state == MeasurementState.PASSED_QUALITY`` before using
    an offset for guiding.
    """

    x: float = float("nan")
    y: float = float("nan")
    snr: float = 0.0
    fwhm: float = float("nan")
    e1: float = float("nan")
    e2: float = float("nan")
    state: MeasurementState = MeasurementState.NOT_SET

    @property
    def is_finite(self) -> bool:
        """Whether both centroid coordinates are finite."""
        return bool(np.isfinite(self.x) and np.isfinite(self.y))

    def offset_from(self, reference: CentroidMeasurement) -> tuple[float, float]:
        """Return ``measured - reference`` in amplifier pixel coordinates.

        Parameters
        ----------
        reference : `CentroidMeasurement`
            Fixed reference in the same stamp coordinates. Only its
            centroid coordinates are used; it need not contain shape
            or quality information.

        Returns
        -------
        dx, dy : `float`
            Displacement along stamp columns and rows, respectively.
            No camera rotation or quality filtering is applied.

        Raises
        ------
        TypeError
            If the reference is not a `CentroidMeasurement`.
        ValueError
            If either centroid has non-finite coordinates.
        """
        if not isinstance(reference, CentroidMeasurement):
            raise TypeError("reference must be a CentroidMeasurement.")
        if not self.is_finite or not reference.is_finite:
            raise ValueError("Cannot calculate offsets with non-finite coordinates.")
        return float(self.x - reference.x), float(self.y - reference.y)
