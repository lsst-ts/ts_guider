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

"""Source detection and background helpers for the guider pipeline.

The centroiding mirrors ``lsst.summit.utils.guiders.detection``
(connected-component detection then HSM validation) so the reference
selection matches the summit pipeline.
"""

from __future__ import annotations

__all__ = [
    "build_reference_image",
    "find_candidate_centroids",
    "find_reference_centroid",
]

import logging

import numpy as np
import scipy.ndimage as ndimage
from astropy.stats import sigma_clipped_stats
from scipy.stats import median_abs_deviation

from .models import GuiderTrackerConfig

log = logging.getLogger(__name__)


def build_reference_image(stamps: np.ndarray, n_seed_frames: int) -> np.ndarray:
    """Return the first stamp, or a dithered median coadd of the first N.

    The uniform [0, 1) dither breaks integer quantization before the
    median, matching ``GuiderData.getStampArrayCoadd`` (DM-54263).
    """
    if n_seed_frames <= 1:
        return stamps[0].astype(np.float32)
    n = min(n_seed_frames, stamps.shape[0])
    stack = stamps[:n].astype(np.float32).copy()
    rng = np.random.default_rng(seed=0)
    stack += rng.uniform(0.0, 1.0, size=stack.shape).astype(np.float32)
    return np.nanmedian(stack, axis=0)


def find_candidate_centroids(
    image: np.ndarray, config: GuiderTrackerConfig
) -> list[tuple[float, float, float]]:
    """List every detected source passing the cheap size and edge cuts.

    Connected-component detection on a MAD-thresholded, background-
    subtracted image: sigma-clipped background, scaled-MAD noise,
    threshold, ``scipy.ndimage`` labelling. Returns ``(center_x,
    center_y, flux)`` tuples in 0-indexed (numpy) pixels, sorted by
    integrated flux (descending).

    This only *locates* candidates; deciding which one is the guide
    star is left to HSM validation in
    :meth:`SensorTracker.lock_reference`. That separation mirrors
    ``summit_utils.guiders.detection`` (detect every footprint, then
    rank by measured SNR) and is what keeps thin detector-edge or
    amplifier-step columns - which can out-flux a compact star but fail
    an adaptive-moment fit - from being chosen.
    """
    _, background_median, _ = sigma_clipped_stats(image, sigma=3.0)
    background_subtracted = image - background_median

    noise = float(
        median_abs_deviation(
            background_subtracted.ravel(), scale="normal", nan_policy="omit"
        )
    )
    if noise <= 0:
        log.warning("Noise estimate is zero; cannot threshold image.")
        return []

    above_threshold = background_subtracted > config.detection_threshold * noise
    labels, n_components = ndimage.label(above_threshold)
    if n_components == 0:
        log.warning("No sources above threshold in reference image.")
        return []

    n_rows, n_cols = image.shape
    candidates: list[tuple[float, float, float]] = []
    for label_id in range(1, n_components + 1):
        component_mask = labels == label_id
        if int(component_mask.sum()) < config.n_pix_min:
            continue
        center_y, center_x = ndimage.center_of_mass(
            background_subtracted * component_mask
        )
        if (
            center_x < config.edge_margin
            or center_x > n_cols - config.edge_margin
            or center_y < config.edge_margin
            or center_y > n_rows - config.edge_margin
        ):
            continue
        component_flux = float(np.nansum(background_subtracted[component_mask]))
        candidates.append((float(center_x), float(center_y), component_flux))

    candidates.sort(key=lambda candidate: candidate[2], reverse=True)
    return candidates


def find_reference_centroid(
    image: np.ndarray, config: GuiderTrackerConfig
) -> tuple[float, float] | None:
    """Return the brightest-by-flux candidate center (legacy helper).

    Kept for callers that just need a single quick detection on one
    image. The tracker no longer uses this - it validates candidates
    with HSM across the seed window instead (see
    :meth:`SensorTracker.lock_reference`).
    """
    candidates = find_candidate_centroids(image, config)
    if not candidates:
        return None
    return (candidates[0][0], candidates[0][1])


def subtract_annulus_background(
    cutout: np.ndarray, inner_radius: float, outer_radius: float
) -> tuple[np.ndarray, float]:
    """Subtract a robust background measured in a circular annulus."""
    center_x = cutout.shape[1] // 2
    center_y = cutout.shape[0] // 2
    grid_y, grid_x = np.indices(cutout.shape)
    radius_squared = (grid_x - center_x) ** 2 + (grid_y - center_y) ** 2
    annulus_mask = (
        (radius_squared >= inner_radius**2)
        & (radius_squared <= outer_radius**2)
        & np.isfinite(cutout)
    )
    if not np.any(annulus_mask):
        return cutout, 1.0
    _, background_median, background_std = sigma_clipped_stats(
        cutout[annulus_mask], sigma=3.0
    )
    return cutout - background_median, float(background_std)
