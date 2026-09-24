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

__all__ = ["SensorTracker"]

import logging
from time import perf_counter

import galsim
import numpy as np
from astropy.nddata import Cutout2D

from .centroid_measurement import CentroidMeasurement
from .detection import Detection
from .guider_tracker_config import GuiderTrackerConfig
from .guiding_status import GuidingStatus
from .measurement_state import MeasurementState

log = logging.getLogger(__name__)


class SensorTracker:
    """Track one guide star on one sensor at a fixed reference window.

    Parameters
    ----------
    sensor_name : `str`
        Sensor identifier used in diagnostics.
    config : `GuiderTrackerConfig`
        Reference selection, measurement, and quality-cut settings.

    Attributes
    ----------
    reference : `CentroidMeasurement` or `None`
        Fixed detected position in the seed reference image, in zero-based
        stamp pixels. Only ``x`` and ``y`` are populated; shape, SNR, and
        state keep their defaults because the reference position
        comes from detection, not an adaptive-moment measurement.
        Its measurement state is NOT_SET.
        `None` until reference locking succeeds.
    stamp_shape : `tuple` [`int`, `int`] or `None`
        Expected stamp shape as ``(rows, columns)``.
    last_measurement : `CentroidMeasurement` or `None`
        Most recent measurement, including a rejected result. For a
        successful seed window, this is the last seed's measurement.
    last_error : `Exception` or `None`
        Processing failure that put the tracker in ERROR. The original
        exception is also raised to the caller.
    lock_duration : `float` or `None`
        Seconds spent selecting a reference in the latest
        ``process_stamp`` call, or `None` if no attempt was made.
    measurement_durations : `tuple` [`float`, ...]
        Measurement times in seconds, in the same order as
        ``get_measurements()``. Candidate-scoring time belongs to
        ``lock_duration`` instead.
    """

    def __init__(self, sensor_name: str, config: GuiderTrackerConfig):
        self.sensor_name = sensor_name
        self._config = config
        self.detection = Detection(config, log=log)
        self.reset()

    @property
    def config(self) -> GuiderTrackerConfig:
        """Mutable settings shared with the detector.

        Update fields directly, or replace the configuration and start a
        new reference window with ``reset(config=...)``.
        """
        return self._config

    @property
    def reference_center(self) -> tuple[float, float] | None:
        """Read-only detected ``(x, y)`` reference for cutout placement.

        Coordinates use zero-based stamp pixels. Return `None` until
        reference locking succeeds.
        """
        if self.reference is None:
            return None
        return self.reference.x, self.reference.y

    def reset(self, config: GuiderTrackerConfig | None = None) -> None:
        """Clear the reference, pending seeds, results, and error state.

        Parameters
        ----------
        config : `GuiderTrackerConfig` or `None`, optional
            Replacement settings. If omitted, retain the current settings.
            The next stamp starts a new reference window in either case.
        """
        if config is not None:
            self._config = config
            self.detection = Detection(config, log=log)
        self.reference: CentroidMeasurement | None = None
        self.stamp_shape: tuple[int, int] | None = None
        self.last_measurement: CentroidMeasurement | None = None
        self.last_error: Exception | None = None
        self.lock_duration: float | None = None
        self.measurement_durations: tuple[float, ...] = ()
        self._status = GuidingStatus.NONE
        self._seed_stamps: list[np.ndarray] = []
        self._measurements: tuple[CentroidMeasurement, ...] = ()

    def get_status(self) -> GuidingStatus:
        """Return the current per-sensor guiding state."""
        return self._status

    def get_offsets(self) -> tuple[float, float]:
        """Return the latest usable displacement in amplifier pixels.

        The sign is ``measured - reference``, along stamp columns and
        rows. Raise `RuntimeError` unless a reference and a latest
        accepted measurement are available in the LOCKED state.
        """
        if (
            self._status != GuidingStatus.LOCKED
            or self.last_measurement is None
            or self.last_measurement.state != MeasurementState.PASSED_QUALITY
            or not self.last_measurement.is_finite
            or self.reference is None
            or not self.reference.is_finite
        ):
            raise RuntimeError(
                f"Sensor {self.sensor_name}: no usable offset "
                f"(status={self._status.name})."
            )
        return self.last_measurement.offset_from(self.reference)

    def get_measurements(self) -> tuple[CentroidMeasurement, ...]:
        """Return measurements produced by the latest processing call.

        While collecting seeds or after a failed lock this is empty.
        A successful lock returns every seed's measurement in input
        order, including rejected measurements. Once a reference exists,
        each call produces one measurement, including rejected results.

        This batch is replaced by the next call, not accumulated
        or consumed by reading it. A streaming caller can pair it with
        the acquisition indices it retained while the seeds arrived.
        """
        return self._measurements

    @property
    def pending_seed_count(self) -> int:
        """Number of stored seed images awaiting successful locking."""
        return len(self._seed_stamps)

    def process_stamp(self, stamp: np.ndarray) -> None:
        """Accumulate a seed, attempt locking, or measure one live stamp.

        Parameters
        ----------
        stamp : `numpy.ndarray`
            Nonempty image with shape ``(rows, columns)``. The shape
            must remain fixed until reset. Buffered images are copied
            so the caller may reuse its input array.

        Notes
        -----
        Collect ``config.seed_frames`` images before the first attempt
        to lock. After an unsuccessful attempt, retain the images and
        retry with each new stamp using all accumulated seeds in LOCKING.
        Once locking succeeds, replay all seeds through ``measure`` and
        release the images. ``get_measurements`` exposes those results
        in input order.

        The full seed buffer (not just its coadd) is used to HSM-validate
        candidates over the seed window. Once locked, the frames are replayed
        through the normal measure path so the offset table covers
        the warm-up stamps too.

        Successful reference selection sets LOCKED. A rejected measurement
        keeps that state and reference, but ``get_offsets`` refuses the
        current result. Tracking state and individual measurement quality
        are separate. Automatic transitions to LOST are not implemented
        until a loss and recovery policy is agreed; no rejection threshold
        is assumed and no star is reselected automatically.

        Malformed inputs and unexpected processing exceptions set ERROR
        and are re-raised. Further processing requires ``reset`` or an
        explicit ``lock_reference`` call. Calls must be serialized.
        """
        if self._status == GuidingStatus.ERROR:
            raise RuntimeError(
                f"Sensor {self.sensor_name}: reset required after processing error."
            ) from self.last_error
        self._measurements = ()
        self.measurement_durations = ()
        self.lock_duration = None
        try:
            stamp = np.asarray(stamp, dtype=np.float32)
            if stamp.ndim != 2 or 0 in stamp.shape:
                raise ValueError("stamp must be a nonempty (rows, columns) image.")
            if self.stamp_shape is not None and stamp.shape != self.stamp_shape:
                raise ValueError(
                    f"Expected stamp shape {self.stamp_shape}; got {stamp.shape}."
                )
            if self.reference_center is not None:
                self.measure(stamp)
                return

            self._status = GuidingStatus.LOCKING
            self.stamp_shape = stamp.shape
            # The callback owns pixels only for this call. Retain a copy
            # even when a direct caller supplies an already-float32 array.
            self._seed_stamps.append(stamp.copy())
            if len(self._seed_stamps) < self.config.seed_frames:
                return

            seed_stamps = np.stack(self._seed_stamps)
            # Retain the seed images after an unsuccessful attempt.
            # The public lock_reference method explicitly resets them.
            if self._try_lock_reference(seed_stamps):
                measurements = []
                durations = []
                for seed in seed_stamps:
                    measurements.append(self.measure(seed))
                    durations.extend(self.measurement_durations)
                self._measurements = tuple(measurements)
                self.measurement_durations = tuple(durations)
                self._seed_stamps.clear()
        except Exception as error:
            self._record_error(error)
            raise

    def _record_error(self, error: Exception) -> None:
        """Latch a failure and release buffered images and stale results."""
        self._status = GuidingStatus.ERROR
        self.last_error = error
        self.last_measurement = None
        self._seed_stamps.clear()
        self._measurements = ()
        self.measurement_durations = ()

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

        Parameters
        ----------
        seed_stamps : `numpy.ndarray`
            Nonempty image cube with shape ``(frames, rows, columns)``.
            All supplied frames are used. The caller controls how many
            stamps to accumulate; ``config.seed_frames`` only sets the
            first automatic attempt in ``process_stamp``. A shorter
            nonempty cube is also accepted by this method.

        Returns
        -------
        locked : `bool`
            Whether a candidate passed the seed-window validity gate.

        Raises
        ------
        ValueError
            If the cube is empty or does not have three dimensions.

        Notes
        -----
        The detected position of the selected candidate becomes the fixed
        reference. Calling this method clears any previous reference,
        pending seeds, measurements, and error state, including when the
        new attempt fails. It sets LOCKED on success and LOCKING on
        failure, but does not replay the seeds. Call ``measure`` before
        requesting offsets, or use ``process_stamp`` for automatic replay.
        """
        self.reset()
        return self._try_lock_reference(seed_stamps)

    def _try_lock_reference(self, seed_stamps: np.ndarray) -> bool:
        """Attempt locking and record its state and duration without reset."""
        self._status = GuidingStatus.LOCKING
        lock_start = perf_counter()
        try:
            locked = self._lock_reference(seed_stamps)
            if locked:
                self._status = GuidingStatus.LOCKED
            return locked
        except Exception as error:
            self._record_error(error)
            raise
        finally:
            self.lock_duration = perf_counter() - lock_start

    def _lock_reference(self, seed_stamps: np.ndarray) -> bool:
        """Select the reference using all supplied seed stamps."""
        seed_stamps = np.asarray(seed_stamps)
        if seed_stamps.ndim != 3 or 0 in seed_stamps.shape:
            raise ValueError(
                "seed_stamps must be a nonempty (frames, rows, columns) cube."
            )
        self.stamp_shape = (seed_stamps.shape[1], seed_stamps.shape[2])
        reference_image = self.detection.build_reference_image(seed_stamps)
        seed_window = seed_stamps

        candidates = self.detection.find_candidate_centroids(reference_image)
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
                if measurement.state == MeasurementState.PASSED_QUALITY
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
        # Preserve the detected center as the fixed reference; the HSM
        # measurements used to score it have a different estimator.
        self.reference = CentroidMeasurement(x=best["center"][0], y=best["center"][1])
        log.info(
            f"Sensor {self.sensor_name}: locked reference at "
            f"({best['center'][0]:.2f}, {best['center'][1]:.2f}) "
            f"[median SNR={best['median_snr']:.1f}, valid "
            f"{best['valid_fraction'] * 100:.0f}% of {len(seed_window)} "
            f"seed frames, {len(candidates)} candidate(s) considered]."
        )
        return True

    def measure(self, stamp: np.ndarray) -> CentroidMeasurement:
        """Measure the HSM centroid of one stamp at the fixed window.

        Parameters
        ----------
        stamp : `numpy.ndarray`
            Image with the same ``(rows, columns)`` shape as the seed data.

        Returns
        -------
        measurement : `CentroidMeasurement`
            Centroid in full-stamp amplifier pixels, shape, and measurement
            state. Call ``measurement.offset_from(tracker.reference)`` for
            the displacement, requiring
            ``measurement.state == MeasurementState.PASSED_QUALITY``
            before guiding. A failed fit or unusable cutout returns a
            NOT_CONVERGED result.

        Raises
        ------
        RuntimeError
            If no reference is locked.
        ValueError
            If the image shape differs from the reference stamps.

        Notes
        -----
        Retained for callers that supply their own seed cube. Records
        the latest measurement while retaining LOCKED and the reference,
        even when that measurement fails quality. Processing exceptions
        set ERROR and are re-raised.
        """
        if self._status == GuidingStatus.ERROR:
            raise RuntimeError(
                f"Sensor {self.sensor_name}: reset required after processing error."
            ) from self.last_error
        try:
            if self.reference_center is None:
                raise RuntimeError(f"Sensor {self.sensor_name}: reference not locked.")
            stamp = np.asarray(stamp)
            if stamp.shape != self.stamp_shape:
                raise ValueError(
                    f"Expected stamp shape {self.stamp_shape}; got {stamp.shape}."
                )
            measure_start = perf_counter()
            measurement = self._measure_at(stamp, self.reference_center)
            self.measurement_durations = (perf_counter() - measure_start,)
            self.last_measurement = measurement
            self._measurements = (measurement,)
            self._status = GuidingStatus.LOCKED
            return measurement
        except Exception as error:
            self._record_error(error)
            raise

    def _measure_at(
        self, stamp: np.ndarray, center: tuple[float, float]
    ) -> CentroidMeasurement:
        """Run the annulus background + HSM fit at a given window center.

        Used both for per-frame tracking (at the locked reference) and
        for scoring candidate references during :meth:`lock_reference`.
        """
        cutout = Cutout2D(
            np.asarray(stamp, dtype=np.float32),
            center,
            size=self.config.cutout_size,
            mode="partial",
            fill_value=np.nan,
        )
        data = cutout.data
        if np.all(data == 0) or not np.isfinite(data).all():
            return CentroidMeasurement(state=MeasurementState.NOT_CONVERGED)

        data_subtracted, background_std = self.detection.subtract_annulus_background(
            data,
            self.config.aperture_radius,
            self.config.aperture_radius * 2.0,
        )

        galsim_image = galsim.Image(data_subtracted.astype(np.float64))
        hsm_result = galsim.hsm.FindAdaptiveMom(galsim_image, strict=False)
        if hsm_result.error_message != "":
            return CentroidMeasurement(state=MeasurementState.NOT_CONVERGED)

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
            state=MeasurementState.CONVERGED,
        )
        if self._passes_quality(measurement):
            measurement.state = MeasurementState.PASSED_QUALITY
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
        """Apply the SNR, ellipticity, size and edge quality cuts."""
        if (
            measurement.state
            not in (
                MeasurementState.CONVERGED,
                MeasurementState.PASSED_QUALITY,
            )
            or not measurement.is_finite
        ):
            return False
        if not np.isfinite(
            [measurement.snr, measurement.fwhm, measurement.e1, measurement.e2]
        ).all():
            return False
        if measurement.snr < self.config.min_snr:
            return False
        if np.hypot(measurement.e1, measurement.e2) > self.config.max_ellipticity:
            return False
        # Reject sources wider than the configured stellar-width limit.
        if not 0 < measurement.fwhm <= self.config.max_fwhm:
            return False
        if self.stamp_shape is None:
            return True
        n_rows, n_cols = self.stamp_shape
        margin = self.config.edge_margin
        return (
            margin <= measurement.x < n_cols - margin
            and margin <= measurement.y < n_rows - margin
        )
