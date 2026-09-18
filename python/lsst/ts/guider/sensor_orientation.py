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

"""Guide-sensor orientation for amplifier-to-camera transforms.

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

Both quantities are read at runtime from the authoritative LSST camera
model (``lsst.obs.lsst.LsstCam``) through :class:`GuiderOrientation`.
The camera is built once and the per-detector values are cached, so
the per-stamp cost stays a flip and a 2x2 rotation.
The camera is imported lazily, so this module (and the two pure helpers
below) import without the LSST stack; only the camera-frame transform
needs it.
"""

from __future__ import annotations

__all__ = [
    "GuiderOrientation",
    "amplifier_name_from_segment",
    "amplifier_name_from_segment_index",
    "detector_name_from_sensor_index",
    "generate_orientation_table",
]

import numpy as np

# Integer rotation matrices for k counter-clockwise quarter turns,
# mirroring ``lsst.summit.utils.guiders`` ROTATION_MATRICES. Row form:
# (camera_dx, camera_dy) = ((a, b), (c, d)) . (dx, dy). This is generic
# math, not camera-specific data; the camera-specific values (nQuarter
# and amplifier flips) come from the camera model at runtime.
_ROTATION_MATRICES: dict[int, tuple[tuple[int, int], tuple[int, int]]] = {
    0: ((1, 0), (0, 1)),
    1: ((0, -1), (1, 0)),
    2: ((-1, 0), (0, -1)),
    3: ((0, 1), (-1, 0)),
}

# GDS ``Location`` packs (bay, board, sensor) into one index as
# bay * (BOARDS_PER_BAY * SENSORS_PER_BOARD) + board * SENSORS_PER_BOARD
# + sensor. The guide sensors sit on board 1 (0 = science,
# 2 = wavefront). See ts_guider/docs/DECODING_RAFT_SENSOR_NAMES.md.
BOARDS_PER_BAY = 3
SENSORS_PER_BOARD = 3
GUIDE_SENSOR_BOARD = 1
RAFT_COLUMNS = 5


def detector_name_from_sensor_index(sensor_index: int) -> str:
    """Decode a GDS ``sensor_index`` to a guide sensor name.

    This recovers the detector identity straight from the per-stamp
    metadata, so the python (CSC) side can name the sensor without any
    DAQ series configuration. It decodes the GDS ``Location`` packing,
    a DAQ wire convention; it is independent of the camera geometry.

    Parameters
    ----------
    sensor_index : `int`
        Packed GDS ``Location`` index from ``StampMetadata``, e.g.
        ``183``.

    Returns
    -------
    detector_name : `str`
        Guide sensor name keyed as in the camera model, e.g.
        ``"R40_SG0"``.

    Raises
    ------
    ValueError
        If the index does not decode to a guide sensor (board 1).
    """
    boards_times_sensors = BOARDS_PER_BAY * SENSORS_PER_BOARD
    bay = sensor_index // boards_times_sensors
    board = (sensor_index % boards_times_sensors) // SENSORS_PER_BOARD
    sensor = sensor_index % SENSORS_PER_BOARD
    if board != GUIDE_SENSOR_BOARD:
        raise ValueError(
            f"sensor_index {sensor_index} decodes to board {board}, "
            f"not a guide sensor (board {GUIDE_SENSOR_BOARD})."
        )
    raft_row = bay // RAFT_COLUMNS
    raft_column = bay % RAFT_COLUMNS
    return f"R{raft_row}{raft_column}_SG{sensor}"


def amplifier_name_from_segment(segment: str) -> str:
    """Return the amplifier name for a ``ROISEG`` metadata value.

    This matches ``lsst.summit.utils.guiders`` ``getAmpNameFromMetadata``
    (``"C" + segment[7:]``).

    Parameters
    ----------
    segment : `str`
        Segment string from the stamp metadata, e.g. ``"Segment05"``.

    Returns
    -------
    amplifier_name : `str`
        Amplifier name, e.g. ``"C05"``.
    """
    return "C" + segment[7:]


def amplifier_name_from_segment_index(segment_index: int) -> str:
    """Return the amplifier name for a GDS segment index.

    The live DAQ path carries the ROI amplifier as the integer
    ``RoiLocation.segment()`` (per-stamp ``metadata.segment``), not the
    ``"Segment05"`` string the FITS ``ROISEG`` header uses. The integer
    follows the amplifier label numbering (``C00``-``C07`` on the lower
    row, ``C10``-``C17`` on the upper), so the amplifier name is ``"C"``
    plus the two-digit index. A value that does not name a real
    amplifier (e.g. a flat 8 or 9) yields a name the camera model
    rejects, so the camera-frame combine falls back to amplifier
    coordinates rather than transforming with the wrong flip.

    Parameters
    ----------
    segment_index : `int`
        GDS ROI segment for the sensor, e.g. ``5`` for ``"C05"``.

    Returns
    -------
    amplifier_name : `str`
        Amplifier name, e.g. ``"C05"``.
    """
    return f"C{segment_index:02d}"


class GuiderOrientation:
    """Amplifier-to-camera transforms read from the LSST camera model.

    The flip and rotation values come from ``lsst.obs.lsst.LsstCam`` at
    runtime, not from a static table. The camera itself is built lazily
    on first access and reused; ``get_nquarter`` and
    ``get_amplifier_flip`` read straight from it (no separate
    memoization - a camera lookup is a few microseconds, negligible
    next to HSM centroiding, and this method is only called once per
    sensor per combine, not per stamp).

    Parameters
    ----------
    camera : `lsst.afw.cameraGeom.Camera`, optional
        Camera to read. Defaults to ``LsstCam.getCamera()``, built on
        first use. Pass an existing camera to share one across the
        pipeline and avoid a second build.
    """

    def __init__(self, camera=None):
        self._camera = camera

    @property
    def camera(self):
        """The camera model, built on first access."""
        if self._camera is None:
            from lsst.obs.lsst import LsstCam

            self._camera = LsstCam.getCamera()
        return self._camera

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


def generate_orientation_table(camera=None) -> dict[str, dict]:
    """Read the guide-sensor orientation table from the camera model.

    Convenience inspector that dumps every guide detector's id,
    ``nQuarter`` and per-amplifier flips. It is the same data
    :class:`GuiderOrientation` reads per detector; use it to inspect or
    verify the model. Requires the LSST stack (``lsst.obs.lsst``).

    Parameters
    ----------
    camera : `lsst.afw.cameraGeom.Camera`, optional
        Camera to read. Defaults to ``LsstCam.getCamera()``.

    Returns
    -------
    table : `dict`
        Mapping of guide sensor name to a dict with ``detector_id``,
        ``nquarter`` and ``amplifier_flip``.
    """
    from lsst.afw import cameraGeom
    from lsst.obs.lsst import LsstCam

    if camera is None:
        camera = LsstCam.getCamera()

    table: dict[str, dict] = {}
    for detector in camera:
        if detector.getType() != cameraGeom.DetectorType.GUIDER:
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


def _print_orientation_table(camera=None) -> None:
    """Print the orientation table read from the camera model."""
    table = generate_orientation_table(camera)
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


if __name__ == "__main__":
    _print_orientation_table()
