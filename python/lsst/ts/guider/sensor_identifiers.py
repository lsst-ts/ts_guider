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

__all__ = ["detector_name_from_sensor_index", "amplifier_name_from_segment_index"]

# GDS ``Location`` packs (bay, board, sensor) into one index as
# bay * (BOARDS_PER_BAY * SENSORS_PER_BOARD) + board * SENSORS_PER_BOARD
# + sensor. The guide sensors sit on board 1 (0 = science,
# 2 = wavefront). See doc/decoding_raft_sensor_names.rst.
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


def amplifier_name_from_segment_index(segment_index: int) -> str:
    """Return the amplifier name for a GDS segment index.

    The live DAQ path carries the ROI amplifier as the integer
    ``RoiLocation.segment()`` (per-stamp ``metadata.segment``), not the
    ``"Segment05"`` string the FITS ``ROISEG`` header uses. The integer
    follows the amplifier label numbering (``C00``-``C07`` on the lower
    row, ``C10``-``C17`` on the upper), so the amplifier name is ``"C"``
    plus the two-digit index. A value that does not name a real
    amplifier (e.g. a flat 8 or 9) yields a name the camera model
    rejects. This helper formats the name without validating it;
    camera lookup errors are handled by ``OffsetCombiner``.

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
