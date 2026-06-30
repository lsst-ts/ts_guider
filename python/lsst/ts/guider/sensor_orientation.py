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

"""Guide-sensor orientation lookup for amplifier-to-camera transforms.

Centroiding runs in amplifier (ROI) pixel space, the orientation in
which the DAQ delivers each stamp. Before combining the eight guide
sensors into a common ``(X, Y)`` camera offset, each sensor must be
flipped and rotated into a shared focal-plane (camera) orientation.

The transform is the one described by Aaron Roodman (April 22, 2026
guider meeting) and implemented in ``lsst.summit.utils.guiders``:

1. Amplifier flip: ``numpy.fliplr`` / ``numpy.flipud`` according to the
   per-amplifier ``getRawFlipX`` / ``getRawFlipY`` of the segment the
   ROI sits on. The segment comes from the ``ROISEG`` stamp metadata.
2. Detector rotation: ``numpy.rot90`` by the detector's ``nQuarter``
   (number of 90 degree turns), fixed per CCD in the camera model.

The authoritative source of these values is the LSST camera model
(``lsst.obs.lsst.LsstCam``). :func:`generate_orientation_table` reads
them from that model, but it requires the LSST stack. The static
tables below mirror that model so the transform also works in
environments without the stack. Use :func:`generate_orientation_table`
to regenerate and verify them whenever the camera model changes.
"""

from __future__ import annotations

__all__ = [
    "GUIDER_DETECTOR_IDS",
    "GUIDER_NQUARTER",
    "AMPLIFIER_RAW_FLIP",
    "amplifier_name_from_segment",
    "detector_name_from_sensor_index",
    "get_nquarter",
    "get_amplifier_flip",
    "amplifier_to_ccd_view",
    "ccd_to_camera_view",
    "amplifier_to_camera_view",
    "amplifier_to_camera_offset",
    "generate_orientation_table",
]

import numpy as np

# Integer rotation matrices for k counter-clockwise quarter turns,
# matching ``lsst.summit.utils.guiders`` ROTATION_MATRICES. Row form:
# (camera_dx, camera_dy) = ((a, b), (c, d)) . (dx, dy).
_ROTATION_MATRICES: dict[int, tuple[tuple[int, int], tuple[int, int]]] = {
    0: ((1, 0), (0, 1)),
    1: ((0, -1), (1, 0)),
    2: ((-1, 0), (0, -1)),
    3: ((0, 1), (-1, 0)),
}

# LsstCam detector id for each guide sensor. The intervening ids
# (191, 192, ...) belong to the wavefront halves of the same corner
# rafts and are intentionally absent here.
GUIDER_DETECTOR_IDS: dict[str, int] = {
    "R00_SG0": 189,
    "R00_SG1": 190,
    "R04_SG0": 193,
    "R04_SG1": 194,
    "R40_SG0": 197,
    "R40_SG1": 198,
    "R44_SG0": 201,
    "R44_SG1": 202,
}

# Raw ``Orientation.getNQuarter()`` per guide sensor. These are passed
# directly to ``numpy.rot90``, which reduces them modulo 4 internally,
# so the raw values (which may exceed 3) are kept as-is to match
# ``lsst.summit.utils.guiders`` exactly.
GUIDER_NQUARTER: dict[str, int] = {
    "R00_SG0": 5,
    "R00_SG1": 2,
    "R04_SG0": 6,
    "R04_SG1": 3,
    "R40_SG0": 4,
    "R40_SG1": 1,
    "R44_SG0": 3,
    "R44_SG1": 0,
}

# Per-amplifier ``(getRawFlipX, getRawFlipY)``. This map is identical
# for all eight guide sensors (verified by
# :func:`generate_orientation_table`): the upper amplifier row
# C10-C17 flips both axes, the lower row C00-C07 flips X only.
AMPLIFIER_RAW_FLIP: dict[str, tuple[bool, bool]] = {
    "C10": (True, True),
    "C11": (True, True),
    "C12": (True, True),
    "C13": (True, True),
    "C14": (True, True),
    "C15": (True, True),
    "C16": (True, True),
    "C17": (True, True),
    "C07": (True, False),
    "C06": (True, False),
    "C05": (True, False),
    "C04": (True, False),
    "C03": (True, False),
    "C02": (True, False),
    "C01": (True, False),
    "C00": (True, False),
}


# GDS ``Location`` packs (bay, board, sensor) into one index as
# bay * (BOARDS_PER_BAY * SENSORS_PER_BOARD) + board * SENSORS_PER_BOARD
# + sensor. The guide sensors sit on board 1 (0 = science,
# 2 = wavefront). See ts_guider/DECODING_RAFT_SENSOR_NAMES.md.
BOARDS_PER_BAY = 3
SENSORS_PER_BOARD = 3
GUIDE_SENSOR_BOARD = 1
RAFT_COLUMNS = 5


def detector_name_from_sensor_index(sensor_index: int) -> str:
    """Decode a GDS ``sensor_index`` to a guide sensor name.

    This recovers the detector identity straight from the per-stamp
    metadata, so the live (CSC) path can look up ``nQuarter`` without
    any series configuration.

    Parameters
    ----------
    sensor_index : `int`
        Packed GDS ``Location`` index from ``StampMetadata``, e.g.
        ``183``.

    Returns
    -------
    detector_name : `str`
        Guide sensor name keyed as in this module, e.g. ``"R40_SG0"``.

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


def get_nquarter(detector_name: str) -> int:
    """Return the raw ``nQuarter`` for a guide sensor.

    Parameters
    ----------
    detector_name : `str`
        Guide sensor name, e.g. ``"R00_SG0"``.

    Returns
    -------
    n_quarter : `int`
        Raw number of 90 degree turns, for use with ``numpy.rot90``.
    """
    return GUIDER_NQUARTER[detector_name]


def get_amplifier_flip(amplifier_name: str) -> tuple[bool, bool]:
    """Return ``(flip_x, flip_y)`` for an amplifier.

    Parameters
    ----------
    amplifier_name : `str`
        Amplifier name, e.g. ``"C05"``.

    Returns
    -------
    flips : `tuple` [`bool`, `bool`]
        Whether to flip in X and in Y to reach CCD orientation.
    """
    return AMPLIFIER_RAW_FLIP[amplifier_name]


def amplifier_to_ccd_view(stamp: np.ndarray, amplifier_name: str) -> np.ndarray:
    """Flip an amplifier-view stamp into CCD orientation.

    Parameters
    ----------
    stamp : `numpy.ndarray`
        Stamp image in amplifier (readout) coordinates.
    amplifier_name : `str`
        Amplifier the ROI sits on, e.g. ``"C05"``.

    Returns
    -------
    ccd_image : `numpy.ndarray`
        Stamp flipped into CCD orientation.
    """
    flip_x, flip_y = get_amplifier_flip(amplifier_name)
    ccd_image = stamp
    if flip_x:
        ccd_image = np.fliplr(ccd_image)
    if flip_y:
        ccd_image = np.flipud(ccd_image)
    return ccd_image


def ccd_to_camera_view(ccd_image: np.ndarray, detector_name: str) -> np.ndarray:
    """Rotate a CCD-view stamp into the common camera orientation.

    Parameters
    ----------
    ccd_image : `numpy.ndarray`
        Stamp image in CCD coordinates.
    detector_name : `str`
        Guide sensor name, e.g. ``"R00_SG0"``.

    Returns
    -------
    camera_image : `numpy.ndarray`
        Stamp rotated into the focal-plane (camera) orientation.
    """
    return np.rot90(ccd_image, -get_nquarter(detector_name))


def amplifier_to_camera_view(
    stamp: np.ndarray, detector_name: str, amplifier_name: str
) -> np.ndarray:
    """Transform a stamp from amplifier view to camera orientation.

    This composes the amplifier flip and the detector rotation, the
    full transform needed before combining sensors into a common
    ``(X, Y)`` offset.

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
    ccd_image = amplifier_to_ccd_view(stamp, amplifier_name)
    return ccd_to_camera_view(ccd_image, detector_name)


def amplifier_to_camera_offset(
    delta_x: float, delta_y: float, detector_name: str, amplifier_name: str
) -> tuple[float, float]:
    """Rotate a per-sensor offset vector into the camera frame.

    This is the vector analogue of :func:`amplifier_to_camera_view`.
    Centroiding stays in amplifier pixels, so a per-sensor offset
    ``(delta_x, delta_y) = (measured - reference)`` is a displacement
    in that amplifier's frame. To average offsets across sensors they
    must share the camera orientation, which needs only the *linear*
    part of the transform: the amplifier flips become sign changes and
    the detector ``nQuarter`` becomes a rotation. The translation that
    the image transform carries cancels in the difference of two
    points, so it is absent here.

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
    flip_x, flip_y = get_amplifier_flip(amplifier_name)
    flipped_x = -delta_x if flip_x else delta_x
    flipped_y = -delta_y if flip_y else delta_y

    (a, b), (c, d) = _ROTATION_MATRICES[get_nquarter(detector_name) % 4]
    camera_x = a * flipped_x + b * flipped_y
    camera_y = c * flipped_x + d * flipped_y
    return float(camera_x), float(camera_y)


def generate_orientation_table(camera=None) -> dict[str, dict]:
    """Build the orientation tables from the LSST camera model.

    This is the source of truth for the static tables in this module.
    It requires the LSST stack (``lsst.obs.lsst``). It also verifies
    that the per-amplifier flip map is identical across all guide
    sensors, raising if that assumption no longer holds.

    Parameters
    ----------
    camera : `lsst.afw.cameraGeom.Camera`, optional
        Camera to read. Defaults to ``LsstCam.getCamera()``.

    Returns
    -------
    table : `dict`
        Mapping of guide sensor name to a dict with ``detector_id``,
        ``nquarter`` and ``amplifier_flip``.

    Raises
    ------
    ValueError
        If the amplifier flip map differs between guide sensors.
    """
    from lsst.afw import cameraGeom
    from lsst.obs.lsst import LsstCam

    if camera is None:
        camera = LsstCam.getCamera()

    table: dict[str, dict] = {}
    shared_amplifier_flip: dict[str, tuple[bool, bool]] | None = None
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
        if shared_amplifier_flip is None:
            shared_amplifier_flip = amplifier_flip
        elif amplifier_flip != shared_amplifier_flip:
            raise ValueError(
                "Amplifier flip map is no longer identical across guide "
                f"sensors; {detector.getName()} differs. The static "
                "AMPLIFIER_RAW_FLIP table must be made per-detector."
            )
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
