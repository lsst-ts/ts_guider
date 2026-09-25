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

__all__ = ["GuiderSequence"]

from dataclasses import dataclass

import numpy as np


@dataclass
class GuiderSequence:
    """All stamps of one guide sequence for a single sensor.

    ``stamps`` has shape ``(frames, rows, columns)``. ``timestamps_mjd``
    contains each stamp's ``STMPTMJD`` header value, or NaN if absent.
    ``segment`` is the FITS ``ROISEG`` value, such as ``"Segment05"``.

    Parameters
    ----------
    stamps : `numpy.ndarray`
        Cube with shape ``(frames, rows, columns)`` in amplifier/readout
        orientation. The FITS reader returns float32 pixels without
        changing the input intensity units.
    timestamps_mjd : `numpy.ndarray`
        One timestamp per frame, shape ``(frames,)``, in MJD days. Missing
        values are NaN. Timestamps do not determine acquisition alignment.
    sensor_name : `str`
        Sensor identity, normally ``RAFTBAY_CCDSLOT`` (e.g. ``R00_SG0``).
        The reader falls back to the filename stem when both are absent.
    segment : `str`, optional
        Original ``ROISEG`` metadata. Camera combination requires the
        form ``SegmentNN`` and an amplifier present in the camera model.
    """

    stamps: np.ndarray
    timestamps_mjd: np.ndarray
    sensor_name: str
    segment: str | None = None

    @property
    def n_stamps(self) -> int:
        """Number of frames in the stamp cube."""
        return self.stamps.shape[0]
