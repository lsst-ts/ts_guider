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

"""Read emulator guider FITS files into a stamp cube."""

from __future__ import annotations

__all__ = ["read_guider_sequence"]

from pathlib import Path

import numpy as np
from astropy.io import fits

from .models import GuiderSequence


def read_guider_sequence(filepath: Path) -> GuiderSequence:
    """Read an emulator guider FITS file into a stamp cube.

    The emulator layout is a metadata-only primary HDU followed by one
    compressed image extension per stamp, each carrying a ``STMPTMJD``
    timestamp. The sensor name is taken from ``RAFTBAY``/``CCDSLOT``.
    """
    with fits.open(filepath) as hdulist:
        primary_header = hdulist[0].header
        raft_bay = primary_header.get("RAFTBAY", "")
        ccd_slot = primary_header.get("CCDSLOT", "")
        sensor_name = f"{raft_bay}_{ccd_slot}".strip("_") or filepath.stem
        roi_segment = primary_header.get("ROISEG")

        frames: list[np.ndarray] = []
        timestamps: list[float] = []
        for hdu in hdulist[1:]:
            if hdu.data is None or hdu.data.ndim != 2:
                continue
            frames.append(hdu.data.astype(np.float32))
            timestamps.append(float(hdu.header.get("STMPTMJD", np.nan)))

    if not frames:
        raise ValueError(f"No image stamps found in '{filepath}'.")

    return GuiderSequence(
        stamps=np.stack(frames, axis=0),
        timestamps_mjd=np.array(timestamps, dtype=float),
        sensor_name=sensor_name,
        segment=str(roi_segment) if roi_segment is not None else None,
    )
