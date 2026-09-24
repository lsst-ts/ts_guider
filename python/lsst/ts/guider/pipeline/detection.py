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

__all__ = ["Detection"]

import logging

import numpy as np
import scipy.ndimage as ndimage
from astropy.stats import sigma_clipped_stats
from scipy.stats import median_abs_deviation

from .guider_tracker_config import GuiderTrackerConfig


class Detection:
    """Detect sources and estimate backgrounds for guider image stamps.

    Parameters
    ----------
    config : `GuiderTrackerConfig`
        Detection threshold, footprint-size, and
        edge-margin settings shared by this detector's methods.
    log : `logging.Logger` or `None`, optional
        Logger for detection diagnostics. If omitted, use this module's
        logger.
    """

    def __init__(
        self, config: GuiderTrackerConfig, log: logging.Logger | None = None
    ) -> None:
        self.config = config
        self.log = log if log is not None else logging.getLogger(__name__)

    def build_reference_image(self, stamps: np.ndarray) -> np.ndarray:
        """Build a float32 reference image from all supplied stamps.

        The uniform [0, 1) dither breaks integer quantization before the
        median, matching ``GuiderData.getStampArrayCoadd`` (DM-54263).
        Use every supplied image. ``config.seed_frames`` controls when
        the tracker first attempts locking, not how many images are used
        here. A cube containing one stamp returns that stamp as float32
        without dithering.

        Parameters
        ----------
        stamps : `numpy.ndarray`
            Nonempty ``(frames, rows, columns)`` image cube.

        Returns
        -------
        reference_image : `numpy.ndarray`
            Float32 reference image with the input frames' shape. Input data
            are not modified; dithering uses a fixed random seed.
        """
        if stamps.shape[0] == 1:
            return stamps[0].astype(np.float32)
        stack = stamps.astype(np.float32).copy()
        rng = np.random.default_rng(seed=0)
        stack += rng.uniform(0.0, 1.0, size=stack.shape).astype(np.float32)
        return np.nanmedian(stack, axis=0)

    def find_candidate_centroids(
        self, image: np.ndarray
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

        Parameters
        ----------
        image : `numpy.ndarray`
            Nonempty two-dimensional reference image.

        Returns
        -------
        candidates : `list` [`tuple` [`float`, `float`, `float`]]
            Candidate x, y, and background-subtracted footprint flux. An empty
            list indicates no detections or an unusable noise estimate.
        """
        config = self.config
        _, background_median, _ = sigma_clipped_stats(image, sigma=3.0)
        background_subtracted = image - background_median

        noise = float(
            median_abs_deviation(
                background_subtracted.ravel(), scale="normal", nan_policy="omit"
            )
        )
        if noise <= 0:
            self.log.warning("Noise estimate is zero; cannot threshold image.")
            return []

        above_threshold = background_subtracted > config.detection_threshold * noise
        labels, n_components = ndimage.label(above_threshold)
        if n_components == 0:
            self.log.warning("No sources above threshold in reference image.")
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

    def find_reference_centroid(self, image: np.ndarray) -> tuple[float, float] | None:
        """Return the brightest-by-flux candidate center (legacy helper).

        Kept for callers that just need a single quick detection on one
        image. The tracker no longer uses this - it validates candidates
        with HSM across the seed window instead (see
        :meth:`SensorTracker.lock_reference`).

        Parameters
        ----------
        image : `numpy.ndarray`
            Nonempty two-dimensional reference image in stamp pixels.

        Returns
        -------
        center : `tuple` [`float`, `float`] or `None`
            Zero-based ``(x, y)`` position of the brightest accepted
            footprint. `None` if no candidate passes the detection cuts.
        """
        candidates = self.find_candidate_centroids(image)
        if not candidates:
            return None
        return (candidates[0][0], candidates[0][1])

    def subtract_annulus_background(
        self, cutout: np.ndarray, inner_radius: float, outer_radius: float
    ) -> tuple[np.ndarray, float]:
        """Subtract a robust background measured in a circular annulus.

        Parameters
        ----------
        cutout : `numpy.ndarray`
            Two-dimensional image cutout, with the annulus centered on its
            central pixel.
        inner_radius : `float`
            Inner annulus radius, in pixels from the central pixel.
        outer_radius : `float`
            Outer annulus radius, in pixels from the central pixel.

        Returns
        -------
        subtracted : `numpy.ndarray`
            Cutout minus the sigma-clipped median annulus background.
        background_std : `float`
            Sigma-clipped standard deviation of the annulus pixels. With no
            usable annulus pixels, return the unchanged cutout and 1.0.
        """
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
