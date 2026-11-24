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

__all__ = [
    "GEOMETRY_SOURCES",
    "GuiderOrientation",
    "amplifier_name_from_segment",
]

import re
from importlib import import_module

import numpy as np

GEOMETRY_SOURCES = ("local", "lsst", "auto")

# LsstCam guide detector name: (detector ID, raw number of quarter turns).
# Verified against obs_lsst w_2026_25. The 16 raw amplifier flips are
# identical on all eight guide detectors in that model.
_LOCAL_DETECTORS = {
    "R00_SG0": (189, 5),
    "R00_SG1": (190, 2),
    "R04_SG0": (193, 6),
    "R04_SG1": (194, 3),
    "R40_SG0": (197, 4),
    "R40_SG1": (198, 1),
    "R44_SG0": (201, 3),
    "R44_SG1": (202, 0),
}
_LOCAL_FLIPS = {
    **{f"C0{i}": (True, False) for i in range(8)},
    **{f"C1{i}": (True, True) for i in range(8)},
}

# Integer rotation matrices for k counter-clockwise quarter turns,
# mirroring ``lsst.summit.utils.guiders`` ROTATION_MATRICES. Row form:
# (camera_dx, camera_dy) = ((a, b), (c, d)) . (dx, dy).
_ROTATION_MATRICES: dict[int, tuple[tuple[int, int], tuple[int, int]]] = {
    0: ((1, 0), (0, 1)),
    1: ((0, -1), (1, 0)),
    2: ((-1, 0), (0, -1)),
    3: ((0, 1), (-1, 0)),
}


def amplifier_name_from_segment(segment: str) -> str:
    """Return the amplifier name for a ``ROISEG`` metadata value.

    For valid metadata this matches ``lsst.summit.utils.guiders``
    ``getAmpNameFromMetadata`` (``"C" + segment[7:]``). The selected geometry
    determines whether the resulting amplifier exists on a detector.

    Parameters
    ----------
    segment : `str`
        ``"Segment"`` followed by two ASCII digits, e.g. ``"Segment05"``.

    Returns
    -------
    amplifier_name : `str`
        Amplifier name, e.g. ``"C05"``.

    Raises
    ------
    ValueError
        If the segment does not have the supported metadata form.
    """
    if (
        not isinstance(segment, str)
        or re.fullmatch(r"Segment[0-9]{2}", segment) is None
    ):
        raise ValueError(f"Expected ROISEG 'SegmentNN'; got {segment!r}.")
    return "C" + segment[7:]


class GuiderOrientation:
    """Transform amplifier pixels and offsets into camera coordinates.

    Centroiding runs in amplifier (ROI) pixel space, the orientation in
    which the DAQ delivers each stamp. Before combining the eight guide
    sensors into a common ``(X, Y)`` camera offset, each sensor must be
    flipped and rotated into a shared focal-plane (camera) orientation.

    The transform is the one described by Aaron Roodman (2026-04-22 guider
    meeting) and implemented in ``lsst.summit.utils.guiders``:

    1. Amplifier flip according to the per-amplifier ``getRawFlipX`` /
       ``getRawFlipY`` of the segment the ROI sits on.
    2. Detector rotation by the detector's ``nQuarter`` (number of 90
       degree turns), fixed per CCD.

    Parameters
    ----------
    camera : `lsst.afw.cameraGeom.Camera`, optional
        An existing LSST camera to use directly.
    geometry_source : `str`, optional
        Use the bundled guider geometry (``local``, the default), require
        the installed camera (``lsst``), or prefer the installed camera
        when available (``auto``). Supplying a camera selects ``lsst``.
        The resolved source is available as ``geometry_source``.
    """

    def __init__(self, camera=None, *, geometry_source: str | None = None):
        if geometry_source is None:
            geometry_source = "local" if camera is None else "lsst"
        if geometry_source not in GEOMETRY_SOURCES:
            raise ValueError(f"Unknown geometry source {geometry_source!r}.")
        if camera is not None and geometry_source == "local":
            raise ValueError("A supplied camera cannot be used with local geometry.")
        if camera is None and geometry_source != "local":
            try:
                obs_lsst = import_module("lsst.obs.lsst")
            except ModuleNotFoundError as error:
                if error.name not in ("lsst.obs", "lsst.obs.lsst"):
                    raise
                if geometry_source == "lsst":
                    raise ModuleNotFoundError(
                        "geometry_source='lsst' requires the obs_lsst camera package.",
                        name=error.name,
                    ) from error
            else:
                camera = obs_lsst.LsstCam.getCamera()
                if camera is None:
                    raise RuntimeError("LsstCam.getCamera() returned no camera.")
        self.camera = camera
        self.geometry_source = "local" if camera is None else "lsst"

    def get_nquarter(self, detector_name: str) -> int:
        """Return the detector ``nQuarter``.

        Parameters
        ----------
        detector_name : `str`
            Guide sensor name, e.g. ``"R00_SG0"``.

        Returns
        -------
        n_quarter : `int`
            Raw number of 90 degree turns, for use with ``numpy.rot90``
            or, reduced modulo 4, with :data:`_ROTATION_MATRICES`.
        """
        if self.camera is None:
            return _LOCAL_DETECTORS[detector_name][1]
        return self.camera[detector_name].getOrientation().getNQuarter()

    def get_amplifier_flip(
        self, detector_name: str, amplifier_name: str
    ) -> tuple[bool, bool]:
        """Return ``(flip_x, flip_y)`` for an amplifier.

        Parameters
        ----------
        detector_name : `str`
            Guide sensor name, e.g. ``"R00_SG0"``.
        amplifier_name : `str`
            Amplifier the ROI sits on, e.g. ``"C05"``.

        Returns
        -------
        flips : `tuple` [`bool`, `bool`]
            Whether to flip in X and in Y to reach CCD orientation.
        """
        if self.camera is None:
            if detector_name not in _LOCAL_DETECTORS:
                raise KeyError(detector_name)
            return _LOCAL_FLIPS[amplifier_name]
        amplifier = self.camera[detector_name][amplifier_name]
        return bool(amplifier.getRawFlipX()), bool(amplifier.getRawFlipY())

    def amplifier_to_camera_offset(
        self,
        delta_x: float,
        delta_y: float,
        detector_name: str,
        amplifier_name: str,
    ) -> tuple[float, float]:
        """Rotate a per-sensor offset vector into the camera frame.

        Centroiding stays in amplifier pixels, so a per-sensor offset
        ``(delta_x, delta_y) = (measured - reference)`` is a
        displacement in that amplifier's frame. To average offsets
        across sensors they must share the camera orientation, which
        needs only the *linear* part of the transform: the amplifier
        flips become sign changes and the detector ``nQuarter`` becomes
        a rotation. The translation that the image transform carries
        cancels in the difference of two points, so it is absent here.

        Parameters
        ----------
        delta_x : `float`
            Offset in X in amplifier pixels.
        delta_y : `float`
            Offset in Y in amplifier pixels.
        detector_name : `str`
            Guide sensor name, e.g. ``"R00_SG0"``.
        amplifier_name : `str`
            Amplifier the ROI sits on, e.g. ``"C05"``.

        Returns
        -------
        camera_offset : `tuple` [`float`, `float`]
            Offset ``(delta_x, delta_y)`` in the common camera frame.
        """
        flip_x, flip_y = self.get_amplifier_flip(detector_name, amplifier_name)
        flipped_x = -delta_x if flip_x else delta_x
        flipped_y = -delta_y if flip_y else delta_y

        (a, b), (c, d) = _ROTATION_MATRICES[self.get_nquarter(detector_name) % 4]
        camera_x = a * flipped_x + b * flipped_y
        camera_y = c * flipped_x + d * flipped_y
        return float(camera_x), float(camera_y)

    def amplifier_to_camera_view(
        self, stamp: np.ndarray, detector_name: str, amplifier_name: str
    ) -> np.ndarray:
        """Transform a stamp image from amplifier view to camera view.

        This is the image analogue of
        :meth:`amplifier_to_camera_offset`, kept for validation against
        ``lsst.summit.utils.guiders`` ``ampToCcdView`` / ``roiImageToDvcs``.
        The pipeline does not use it on the real-time path; offsets are
        transformed as vectors, once, at the combine step.

        Parameters
        ----------
        stamp : `numpy.ndarray`
            Stamp image in amplifier (readout) coordinates.
        detector_name : `str`
            Guide sensor name, e.g. ``"R00_SG0"``.
        amplifier_name : `str`
            Amplifier the ROI sits on, e.g. ``"C05"``.

        Returns
        -------
        camera_image : `numpy.ndarray`
            Stamp in the common focal-plane (camera) orientation.
        """
        flip_x, flip_y = self.get_amplifier_flip(detector_name, amplifier_name)
        ccd_image = stamp
        if flip_x:
            ccd_image = np.fliplr(ccd_image)
        if flip_y:
            ccd_image = np.flipud(ccd_image)
        return np.rot90(ccd_image, -self.get_nquarter(detector_name))

    def generate_orientation_table(self) -> dict[str, dict]:
        """Return the guide-sensor orientation table for the selected source.

        Inspect each guide detector's id, ``nQuarter`` and per-amplifier
        flips used by the offset and image transforms.

        Returns
        -------
        table : `dict`
            Mapping of guide sensor name to a dict with ``detector_id``,
            ``nquarter`` and ``amplifier_flip``.
        """
        if self.camera is None:
            return {
                name: {
                    "detector_id": detector_id,
                    "nquarter": nquarter,
                    "amplifier_flip": _LOCAL_FLIPS.copy(),
                }
                for name, (detector_id, nquarter) in _LOCAL_DETECTORS.items()
            }

        table: dict[str, dict] = {}
        for detector in self.camera:
            if detector.getType().name != "GUIDER":
                continue
            amplifier_flip = {
                amplifier.getName(): (
                    bool(amplifier.getRawFlipX()),
                    bool(amplifier.getRawFlipY()),
                )
                for amplifier in detector
            }
            table[detector.getName()] = {
                "detector_id": detector.getId(),
                "nquarter": detector.getOrientation().getNQuarter(),
                "amplifier_flip": amplifier_flip,
            }
        return table

    def print_orientation_table(self) -> None:
        """Print this camera's guide-sensor orientation table to stdout."""
        table = self.generate_orientation_table()
        header = "{:10s} {:>5s} {:>8s}   per-amp flip (flipX,flipY)".format(
            "detector", "detId", "nQuarter"
        )
        print(header)
        print("-" * 72)
        for detector_name, entry in table.items():
            amp_flips = ", ".join(
                "{}:{}{}".format(name, int(flip_x), int(flip_y))
                for name, (flip_x, flip_y) in entry["amplifier_flip"].items()
            )
            print(
                "{:10s} {:5d} {:8d}   {}".format(
                    detector_name,
                    entry["detector_id"],
                    entry["nquarter"],
                    amp_flips,
                )
            )
