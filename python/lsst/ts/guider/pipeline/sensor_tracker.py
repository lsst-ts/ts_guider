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

"""Per-sensor guide-star tracking: reference lock plus HSM centroiding."""

from __future__ import annotations

__all__ = ["SensorTracker"]

import logging

import galsim
import numpy as np
from astropy.nddata import Cutout2D

from .detection import (
    build_reference_image,
    find_candidate_centroids,
    subtract_annulus_background,
)
from .models import CentroidMeasurement, GuiderTrackerConfig

log = logging.getLogger(__name__)


class SensorTracker:
    """Track one guide star on one sensor at a fixed reference window."""

    def __init__(self, sensor_name: str, config: GuiderTrackerConfig):
        self.sensor_name = sensor_name
        self.config = config
        self.reference_center: tuple[float, float] | None = None
        self.stamp_shape: tuple[int, int] | None = None

    def lock_reference(self, seed_stamps: np.ndarray) -> bool:
        """Detect candidates and lock the best HSM-validated guide star.

        Locking is the most failure-prone step. A bright detector edge
        or an amplifier-boundary step can out-flux a compact star, and a
        single coadd cannot tell whether a candidate is a real, stably
        trackable source. We therefore mirror ``summit_utils`` and
        *measure* candidates rather than trust raw flux:

          1. ``find_candidate_centroids`` lists every blob passing the
             cheap size + edge cuts.
          2. each candidate is tracked with HSM across the seed window
             and must clear the SNR / ellipticity / edge quality cuts on
             at least ``min_valid_stamp_fraction`` of those frames
             (summit's ``minValidStampFraction`` gate).
          3. candidates are ranked by their median seed-window SNR and
             the best one passing the validity gate is locked.

        Why validate over the *seed window* rather than the whole
        sequence?  ``summit_utils`` runs offline with every stamp in
        hand, so it validates each candidate over the entire FITS file
        (``trackStarAcrossStamp``). In a live DAQ stream the future
        stamps do not exist yet at lock time; the only multi-frame
        evidence available is the seed buffer accumulated during
        warm-up. Validating over that window is the streaming-compatible
        equivalent (option 2).

        Not implemented - option 3, left deliberately open: *online
        adaptive re-lock*. Commit to the best candidate, then if it
        accumulates excessive rejects over the next live frames, fall
        back to the next candidate and re-lock mid-stream. That is the
        only way to reproduce summit's full-sequence guarantee on a true
        stream, but it is stateful and deferred until a real need
        appears.

        Returns True when a reference position was locked.
        """
        self.stamp_shape = (seed_stamps.shape[1], seed_stamps.shape[2])
        reference_image = build_reference_image(seed_stamps, self.config.seed_frames)
        seed_window = seed_stamps[: self.config.seed_frames]

        candidates = find_candidate_centroids(reference_image, self.config)
        if not candidates:
            log.warning("Sensor %s: no detection candidates.", self.sensor_name)
            return False

        scored = []
        for center_x, center_y, flux in candidates:
            center = (center_x, center_y)
            valid_snrs = [
                measurement.snr
                for measurement in (
                    self._measure_at(frame, center) for frame in seed_window
                )
                if measurement.passed_quality
            ]
            scored.append(
                {
                    "center": center,
                    "valid_fraction": len(valid_snrs) / len(seed_window),
                    "median_snr": (float(np.median(valid_snrs)) if valid_snrs else 0.0),
                }
            )

        scored.sort(key=lambda s: s["median_snr"], reverse=True)
        accepted = [
            s
            for s in scored
            if s["valid_fraction"] >= self.config.min_valid_stamp_fraction
        ]
        if not accepted:
            log.warning(
                f"Sensor {self.sensor_name}: no candidate cleared the "
                f"{self.config.min_valid_stamp_fraction * 100:.0f}% "
                f"seed-window validity gate over "
                f"{len(seed_window)} frames; not locking "
                f"(best valid fraction "
                f"{max(s['valid_fraction'] for s in scored) * 100:.0f}%)."
            )
            return False

        best = accepted[0]
        self.reference_center = best["center"]
        log.info(
            f"Sensor {self.sensor_name}: locked reference at "
            f"({best['center'][0]:.2f}, {best['center'][1]:.2f}) "
            f"[median SNR={best['median_snr']:.1f}, valid "
            f"{best['valid_fraction'] * 100:.0f}% of {len(seed_window)} "
            f"seed frames, {len(candidates)} candidate(s) considered]."
        )
        return True

    def measure(self, stamp: np.ndarray) -> CentroidMeasurement:
        """Measure the HSM centroid of one stamp at the fixed window."""
        if self.reference_center is None:
            raise RuntimeError(f"Sensor {self.sensor_name}: reference not locked.")
        return self._measure_at(stamp, self.reference_center)

    def _measure_at(
        self, stamp: np.ndarray, center: tuple[float, float]
    ) -> CentroidMeasurement:
        """Run the annulus background + HSM fit at a given window center.

        Used both for per-frame tracking (at the locked reference) and
        for scoring candidate references during :meth:`lock_reference`.
        """
        cutout = Cutout2D(
            stamp,
            center,
            size=self.config.cutout_size,
            mode="partial",
            fill_value=np.nan,
        )
        data = cutout.data
        if np.all(data == 0) or not np.isfinite(data).all():
            return CentroidMeasurement()

        data_subtracted, background_std = subtract_annulus_background(
            data,
            self.config.aperture_radius,
            self.config.aperture_radius * 2.0,
        )

        galsim_image = galsim.Image(data_subtracted.astype(np.float64))
        hsm_result = galsim.hsm.FindAdaptiveMom(galsim_image, strict=False)
        if hsm_result.error_message != "":
            return CentroidMeasurement()

        # GalSim centroids are 1-indexed; shift to 0-indexed numpy pixels.
        centroid_x = hsm_result.moments_centroid.x - 1.0
        centroid_y = hsm_result.moments_centroid.y - 1.0

        signal_to_noise = self._aperture_snr(
            data_subtracted, centroid_x, centroid_y, background_std
        )
        sigma = hsm_result.moments_sigma

        measurement = CentroidMeasurement(
            x=float(centroid_x + cutout.xmin_original),
            y=float(centroid_y + cutout.ymin_original),
            snr=float(signal_to_noise),
            fwhm=float(2.355 * sigma),
            e1=float(hsm_result.observed_shape.e1),
            e2=float(hsm_result.observed_shape.e2),
            converged=True,
        )
        measurement.passed_quality = self._passes_quality(measurement)
        return measurement

    def _aperture_snr(
        self,
        data_subtracted: np.ndarray,
        centroid_x: float,
        centroid_y: float,
        background_std: float,
    ) -> float:
        """Aperture-photometry signal-to-noise (mirrors summit)."""
        grid_y, grid_x = np.indices(data_subtracted.shape)
        aperture_mask = (grid_x - centroid_x) ** 2 + (
            grid_y - centroid_y
        ) ** 2 <= self.config.aperture_radius**2
        flux = max(float(np.nansum(data_subtracted[aperture_mask])), 0.0)
        n_pixels = int(aperture_mask.sum())
        flux_error = float(
            np.sqrt(flux / self.config.gain + n_pixels * background_std**2)
        )
        return flux / flux_error if flux_error > 0 else 0.0

    def _passes_quality(self, measurement: CentroidMeasurement) -> bool:
        """Apply the summit-style SNR, ellipticity and edge cuts."""
        if not measurement.is_finite:
            return False
        if measurement.snr < self.config.min_snr:
            return False
        if np.hypot(measurement.e1, measurement.e2) > self.config.max_ellipticity:
            return False
        if self.stamp_shape is None:
            return True
        n_cols, n_rows = self.stamp_shape
        margin = self.config.edge_margin
        return (
            margin <= measurement.x <= n_cols - margin
            and margin <= measurement.y <= n_rows - margin
        )
