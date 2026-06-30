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

"""Guider tracking pipeline.

The per-sensor centroiding and multi-sensor combine, shared by the
offline FITS driver, the DAQ streaming demo, and the Guider CSC. The
modules depend only on numpy/galsim/scipy/astropy (no salobj), so the
algorithm can be imported and exercised without the CSC.
"""

from .batch import MultiSensorRunner, run_offline
from .detection import (
    build_reference_image,
    find_candidate_centroids,
    find_reference_centroid,
)
from .fits_io import read_guider_sequence
from .models import (
    CentroidMeasurement,
    CombinedOffset,
    GuiderSequence,
    GuiderTrackerConfig,
)
from .offset_combiner import OffsetCombiner, build_sensor_amplifiers
from .sensor_tracker import SensorTracker
from .streaming import StreamingGuiderProcessor, StreamingMetrics

__all__ = [
    "MultiSensorRunner",
    "run_offline",
    "build_reference_image",
    "find_candidate_centroids",
    "find_reference_centroid",
    "read_guider_sequence",
    "CentroidMeasurement",
    "CombinedOffset",
    "GuiderSequence",
    "GuiderTrackerConfig",
    "OffsetCombiner",
    "build_sensor_amplifiers",
    "SensorTracker",
    "StreamingGuiderProcessor",
    "StreamingMetrics",
]
