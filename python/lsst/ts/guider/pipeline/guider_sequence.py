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

from pathlib import Path
from typing import Self

import numpy as np
from astropy.io import fits


class GuiderSequence:
    """All stamps of one guide sequence for a single sensor.

    ``stamps`` has shape ``(frames, rows, columns)``. ``timestamps_mjd``
    contains each stamp's ``STMPTMJD`` header value, or NaN if absent.
    ``segment`` is the FITS ``ROISEG`` value, such as ``"Segment05"``.
    Use :meth:`from_fits` to construct a sequence from a FITS file.

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

    def __init__(
        self,
        stamps: np.ndarray,
        timestamps_mjd: np.ndarray,
        sensor_name: str,
        segment: str | None = None,
    ):
        self.stamps = stamps
        self.timestamps_mjd = timestamps_mjd
        self.sensor_name = sensor_name
        self.segment = segment

    @property
    def n_stamps(self) -> int:
        """Number of frames in the stamp cube."""
        return self.stamps.shape[0]

    @classmethod
    def from_fits(cls, filepath: Path) -> Self:
        """Read an emulator guider FITS file into a stamp cube.

        The emulator layout is a metadata-only primary HDU followed by one
        compressed image extension per stamp, each carrying a ``STMPTMJD``
        timestamp. The sensor name is taken from ``RAFTBAY``/``CCDSLOT``.

        Parameters
        ----------
        filepath : `pathlib.Path`
            FITS file to read. Compressed image extensions are supported.

        Returns
        -------
        sequence : `GuiderSequence`
            Float32 stamp cube in extension order, timestamps in MJD (NaN
            when absent), sensor name, and optional ``ROISEG`` metadata.
            If both sensor-name header fields are absent, use the filename
            stem. Segment metadata is retained for later camera validation.

        Raises
        ------
        ValueError
            If no two-dimensional image extensions exist, their shapes
            differ, or a supplied timestamp cannot be converted to a float.
            FITS opening/reading errors propagate.

        Notes
        -----
        Empty and non-2D extensions are skipped. No timestamp matching or
        acquisition realignment occurs. A missing acquisition in one file
        therefore invalidates index alignment with other sensor files.
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
                if frames and hdu.data.shape != frames[0].shape:
                    raise ValueError(
                        f"Incompatible stamp shapes in {str(filepath)!r}: "
                        f"{frames[0].shape} and {hdu.data.shape}."
                    )
                frames.append(hdu.data.astype(np.float32))
                timestamps.append(float(hdu.header.get("STMPTMJD", np.nan)))

        if not frames:
            raise ValueError(f"No image stamps found in '{filepath}'.")

        return cls(
            stamps=np.stack(frames, axis=0),
            timestamps_mjd=np.array(timestamps, dtype=float),
            sensor_name=sensor_name,
            segment=str(roi_segment) if roi_segment is not None else None,
        )
