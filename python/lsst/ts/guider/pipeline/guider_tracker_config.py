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

__all__ = ["GuiderTrackerConfig"]

from dataclasses import dataclass


@dataclass
class GuiderTrackerConfig:
    """Settings for reference selection and single-sensor guide-star tracking.

    These settings control the initial seed window, source detection,
    adaptive-moment measurement, and acceptance of individual centroids.
    A tracker and its detector share this configuration object. Fields
    can be updated directly, and subsequent processing uses their current
    values. Call ``SensorTracker.reset()`` to start a new reference window
    with the updated settings, or pass a replacement configuration to
    ``SensorTracker.reset(config=...)``.

    Parameters
    ----------
    seed_frames : `int`
        Number of initial stamps to collect before the first reference
        selection attempt. After an unsuccessful attempt, retain the seeds
        and retry with each new stamp using all accumulated images.
    cutout_size : `int`
        Side length of the square centroiding cutout, in pixels.
    aperture_radius : `float`
        Photometric aperture radius, in pixels. The background annulus
        extends from this radius to twice this radius, within the cutout.
    gain : `float`
        Detector gain in electrons per input image unit.
    detection_threshold : `float`
        Detection threshold in units of the reference-image noise.
    edge_margin : `int`
        Excluded margin at each stamp edge, in pixels.
    n_pix_min : `int`
        Minimum number of connected above-threshold pixels in a source.
    min_snr : `float`
        Minimum aperture signal-to-noise ratio for an accepted centroid.
    max_ellipticity : `float`
        Maximum distortion magnitude, ``hypot(e1, e2)``.
    max_fwhm : `float`
        Maximum source full width at half maximum, in pixels.
    min_valid_stamp_fraction : `float`
        Minimum fraction of seed stamps passing quality cuts for a
        candidate reference.
    """

    seed_frames: int = 10
    cutout_size: int = 50
    aperture_radius: float = 10.0
    gain: float = 1.0
    detection_threshold: float = 10.0
    edge_margin: int = 5
    n_pix_min: int = 10
    min_snr: float = 10.0
    max_ellipticity: float = 0.7
    # Reject sources wider than a star (blends, galaxies, hot columns)
    # even when they are bright enough to pass the SNR cut. Guide-star
    # FWHM is a few pixels; a lock at tens of pixels is not a star.
    max_fwhm: float = 15.0
    min_valid_stamp_fraction: float = 0.5
