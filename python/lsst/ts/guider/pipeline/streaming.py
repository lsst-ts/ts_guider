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

"""Streaming guider processor: one stamp at a time from the DAQ.

This is the stamp-callback adapter shared by the CSC and the DAQ
streaming demo. One :class:`SensorTracker` per ``sensor_index`` warms
up, locks, then measures. Once locked, an acquisition (all sensors'
measurements for one ``stamp_index``) is combined and emitted as soon
as a later live ``stamp_index`` starts, so the combined offset is
available live, frame by frame, while the stream runs. The seed
warm-up frames (replayed in bulk at lock time, hence out of global
order) and the final open frame are combined by :meth:`finalize` when
the stream stops.
"""

from __future__ import annotations

__all__ = ["StreamingMetrics", "StreamingGuiderProcessor"]

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from time import perf_counter

import numpy as np

from .. import sensor_orientation
from ..sensor_orientation import GuiderOrientation
from .models import CombinedOffset, GuiderTrackerConfig
from .offset_combiner import OffsetCombiner
from .sensor_tracker import SensorTracker

log = logging.getLogger(__name__)


@dataclass
class StreamingMetrics:
    """Timing and counts accumulated while the stream is running."""

    total_stamps: int = 0
    seed_stamps: int = 0
    valid_measurements: int = 0
    invalid_measurements: int = 0
    measure_times: list[float] = field(default_factory=list)
    lock_times: list[float] = field(default_factory=list)
    callback_times: list[float] = field(default_factory=list)
    combine_times: list[float] = field(default_factory=list)
    first_stamp_time: float | None = None
    last_stamp_time: float | None = None

    @property
    def stream_seconds(self) -> float:
        if self.first_stamp_time is None or self.last_stamp_time is None:
            return 0.0
        return self.last_stamp_time - self.first_stamp_time


def summarize_milliseconds(durations: list[float]) -> str:
    if not durations:
        return "n=0"
    array_ms = np.array(durations) * 1e3
    return (
        f"n={array_ms.size} mean={array_ms.mean():.3f} "
        f"median={np.median(array_ms):.3f} "
        f"p95={np.percentile(array_ms, 95):.3f} "
        f"max={array_ms.max():.3f} ms"
    )


class StreamingGuiderProcessor:
    """Per-stamp DAQ callback target reusing the offline pipeline.

    One ``SensorTracker`` per ``sensor_index``. Warms up, locks, then
    measures. Each locked acquisition is combined live, as soon as the
    next live ``stamp_index`` starts (see :meth:`_maybe_combine_previous`);
    pass ``on_combined_offset`` to be notified with each one as it is
    produced (e.g. to publish telemetry or print a live trace).
    :meth:`finalize` combines the seed warm-up frames and the final
    open frame once the stream stops.
    """

    def __init__(
        self,
        config: GuiderTrackerConfig,
        sensor_names: dict[int, str] | None = None,
        progress_interval: int = 0,
        sensor_amplifiers: dict[str, str] | None = None,
        orientation: GuiderOrientation | None = None,
        on_combined_offset: Callable[[CombinedOffset], None] | None = None,
    ):
        self.config = config
        self.sensor_names = sensor_names or {}
        self.progress_interval = progress_interval
        self.trackers: dict[str, SensorTracker] = {}
        self.seed_buffers: dict[str, list[np.ndarray]] = {}
        self.seed_indices: dict[str, list[int]] = {}
        self.acquisitions: dict[int, dict[str, object]] = {}
        self.combiner = OffsetCombiner(sensor_amplifiers, orientation)
        self.combined_offsets: list[CombinedOffset] = []
        self.metrics = StreamingMetrics()
        self.on_combined_offset = on_combined_offset
        self._latest_stamp_index: int | None = None
        self._combined_indices: set[int] = set()

    def set_sensor_amplifiers(self, sensor_amplifiers: dict[str, str]) -> None:
        """Set the per-sensor amplifier map used for the camera combine.

        Populated from the ROI configuration (segment per sensor). With
        it the per-acquisition combine runs in the camera frame; without
        it the combine stays in amplifier coordinates.
        """
        self.combiner.sensor_amplifiers = sensor_amplifiers

    def _sensor_name(self, sensor_index: int) -> str:
        """Resolve a sensor name for a GDS ``sensor_index``.

        Replay supplies explicit names from the FITS headers. Live has
        only the packed index, so it is decoded to the detector name
        (e.g. ``R40_SG0``) - the key the orientation tables use. This
        makes per-sensor names meaningful in the live logs and lets the
        camera-frame transform resolve once the per-sensor amplifier
        map is provided (the camera combine still needs that map).
        """
        if sensor_index in self.sensor_names:
            return self.sensor_names[sensor_index]
        try:
            return sensor_orientation.detector_name_from_sensor_index(sensor_index)
        except ValueError:
            return f"sensor_{sensor_index:02d}"

    def _record_amplifier(self, sensor_name: str, metadata) -> None:
        """Populate the per-sensor amplifier map from the stamp metadata.

        The ROI segment (amplifier) is constant across a sensor's
        series, so it is recorded once, the first time the sensor is
        seen. This lets the camera-frame combine work in the live path
        without any series configuration: the flip and rotation are
        resolved from the camera model at combine time. An explicit map
        supplied to the constructor is left untouched.

        The GDS segment defaults to 0 (``C00``) until the series
        ``start()`` caches the real value, so a stamp seen before its
        start (e.g. a subscriber joining mid-series) can record ``C00``.
        In practice ``start()`` precedes the sensor's stamps.
        """
        if sensor_name in self.combiner.sensor_amplifiers:
            return
        segment = getattr(metadata, "segment", None)
        if segment is None:
            return
        self.combiner.sensor_amplifiers[sensor_name] = (
            sensor_orientation.amplifier_name_from_segment_index(int(segment))
        )

    @property
    def references(self) -> dict[str, tuple[float, float]]:
        return {
            name: tracker.reference_center
            for name, tracker in self.trackers.items()
            if tracker.reference_center is not None
        }

    def on_stamp(self, pixels: np.ndarray, metadata) -> None:
        """DAQ callback. Runs on the C++ worker thread when live."""
        callback_start = perf_counter()
        self.metrics.total_stamps += 1
        if self.metrics.first_stamp_time is None:
            self.metrics.first_stamp_time = callback_start

        sensor_name = self._sensor_name(int(metadata.sensor_index))
        self._record_amplifier(sensor_name, metadata)
        stamp_index = int(metadata.stamp_index)
        stamp = np.asarray(pixels, dtype=np.float32)

        tracker = self.trackers.get(sensor_name)
        if tracker is None:
            tracker = SensorTracker(sensor_name, self.config)
            self.trackers[sensor_name] = tracker
            self.seed_buffers[sensor_name] = []
            self.seed_indices[sensor_name] = []

        if tracker.reference_center is None:
            self.metrics.seed_stamps += 1
            self.seed_buffers[sensor_name].append(stamp)
            self.seed_indices[sensor_name].append(stamp_index)
            if len(self.seed_buffers[sensor_name]) >= self.config.seed_frames:
                self._lock_and_replay_seed(sensor_name, tracker)
        else:
            self._measure_and_store(
                sensor_name,
                tracker,
                stamp_index,
                stamp,
                receipt_time=callback_start,
            )

        end = perf_counter()
        self.metrics.callback_times.append(end - callback_start)
        self.metrics.last_stamp_time = end

        if (
            self.progress_interval > 0
            and self.metrics.total_stamps % self.progress_interval == 0
        ):
            self._log_progress()

    def _log_progress(self) -> None:
        metrics = self.metrics
        rate = (
            metrics.total_stamps / metrics.stream_seconds
            if metrics.stream_seconds > 0
            else 0.0
        )
        log.info(
            f"progress: {metrics.total_stamps} stamps "
            f"({rate:.0f}/s), {len(self.references)} sensors locked, "
            f"measurements {metrics.valid_measurements} valid / "
            f"{metrics.invalid_measurements} rejected"
        )

    def _lock_and_replay_seed(self, sensor_name: str, tracker: SensorTracker) -> None:
        """Lock the reference, then measure the buffered seed frames.

        The full seed buffer (not just its coadd) is handed to
        ``lock_reference`` so it can HSM-validate candidates over the
        seed window - the streaming-compatible reference selection
        described in the module docstring (option 2). Once locked, the
        buffered frames are replayed through the normal measure path so
        the offset table covers the warm-up stamps too.
        """
        seed_stamps = self.seed_buffers[sensor_name]
        seed_indices = self.seed_indices[sensor_name]
        lock_start = perf_counter()
        locked = tracker.lock_reference(np.stack(seed_stamps))
        self.metrics.lock_times.append(perf_counter() - lock_start)
        if locked:
            for stamp_index, stamp in zip(seed_indices, seed_stamps):
                self._measure_and_store(sensor_name, tracker, stamp_index, stamp)
        self.seed_buffers[sensor_name] = []
        self.seed_indices[sensor_name] = []

    def _measure_and_store(
        self,
        sensor_name: str,
        tracker: SensorTracker,
        stamp_index: int,
        stamp: np.ndarray,
        receipt_time: float | None = None,
    ) -> None:
        measure_start = perf_counter()
        measurement = tracker.measure(stamp)
        self.metrics.measure_times.append(perf_counter() - measure_start)
        self.acquisitions.setdefault(stamp_index, {})[sensor_name] = measurement
        if measurement.passed_quality:
            self.metrics.valid_measurements += 1
        else:
            self.metrics.invalid_measurements += 1
        # Only live stamps (receipt_time set) drive the live combine.
        # Seed-replay frames (receipt_time is None) arrive in bulk and
        # out of global stamp order as each sensor locks, so combining
        # on them would emit single-sensor offsets and drop the frames
        # of later-locking sensors; finalize() combines them instead.
        if receipt_time is not None:
            self._maybe_combine_previous(stamp_index)

        if log.isEnabledFor(logging.DEBUG):
            # Per-sensor offset from the locked reference: this is the
            # dx/dy available the instant measure() returns, ahead of
            # the multi-sensor combine for this acquisition.
            reference_x, reference_y = tracker.reference_center
            offset_x = measurement.x - reference_x
            offset_y = measurement.y - reference_y
            # Latency from stamp receipt (callback entry) to dx/dy when
            # the live path supplies receipt_time; otherwise (seed replay
            # at lock time) just the measure cost.
            elapsed_ms = (
                (perf_counter() - receipt_time)
                if receipt_time is not None
                else (perf_counter() - measure_start)
            ) * 1e3
            log.debug(
                "%s idx=%d dx=%+.3f dy=%+.3f snr=%.1f fwhm=%.2f %s " "(%.2f ms)",
                sensor_name,
                stamp_index,
                offset_x,
                offset_y,
                measurement.snr,
                measurement.fwhm,
                "ok" if measurement.passed_quality else "reject",
                elapsed_ms,
            )

    def _maybe_combine_previous(self, stamp_index: int) -> None:
        """Combine the previous acquisition once a newer live one starts.

        Called only for live stamps (see :meth:`_measure_and_store`).
        Once locked, sensors deliver a ``stamp_index`` in a tight burst
        (all guide sensors, then the next index), so a higher live
        ``stamp_index`` means the previous one is complete and can be
        combined immediately instead of waiting for the stream to stop.
        This is what makes the combined offset available live, frame by
        frame.
        """
        if self._latest_stamp_index is None:
            self._latest_stamp_index = stamp_index
            return
        if stamp_index <= self._latest_stamp_index:
            return
        previous_index = self._latest_stamp_index
        self._latest_stamp_index = stamp_index
        self._combine_acquisition(previous_index, notify=True)

    def _combine_acquisition(self, stamp_index: int, notify: bool) -> None:
        """Combine one acquisition, optionally notifying the callback.

        ``notify`` fires ``on_combined_offset`` and is set for frames
        completed live; :meth:`finalize` combines its trailing frames
        with ``notify=False`` as end-of-stream cleanup.

        A late measurement for an already-combined ``stamp_index``
        (e.g. delivered out of order) reopens an entry in
        ``self.acquisitions``; ``self._combined_indices`` guards against
        combining it twice.
        """
        if stamp_index in self._combined_indices:
            return
        measurements = self.acquisitions.pop(stamp_index, None)
        if not measurements:
            return
        combine_start = perf_counter()
        combined = self.combiner.combine(stamp_index, measurements, self.references)
        self.metrics.combine_times.append(perf_counter() - combine_start)
        self.combined_offsets.append(combined)
        self._combined_indices.add(stamp_index)
        if notify and self.on_combined_offset is not None:
            self.on_combined_offset(combined)

    def finalize(self) -> None:
        """Combine the acquisitions still pending when the stream
        stopped - the seed warm-up frames and the final open frame -
        then order the record by ``stamp_index``.

        These trailing combines do not fire ``on_combined_offset``:
        that callback reports frames completed live while streaming,
        whereas finalize is end-of-stream cleanup.
        """
        for stamp_index in sorted(self.acquisitions):
            self._combine_acquisition(stamp_index, notify=False)
        self.combined_offsets.sort(key=lambda offset: offset.stamp_index)

    def report(self) -> None:
        metrics = self.metrics
        locked = sorted(self.references)
        print("\n=== DAQ guider pipeline metrics ===")
        print(
            f"stamps received      : {metrics.total_stamps}\n"
            f"seed stamps (warm-up): {metrics.seed_stamps}\n"
            f"sensors locked       : {len(locked)} "
            f"({', '.join(locked) if locked else 'none'})\n"
            f"measurements         : "
            f"{metrics.valid_measurements} valid / "
            f"{metrics.invalid_measurements} rejected\n"
            f"acquisitions combined: {len(self.combined_offsets)}"
        )
        print(
            "\n--- timing ---\n"
            f"reference lock : {summarize_milliseconds(metrics.lock_times)}\n"
            f"per-stamp HSM  : "
            f"{summarize_milliseconds(metrics.measure_times)}\n"
            f"callback total : "
            f"{summarize_milliseconds(metrics.callback_times)}\n"
            f"per-acq combine: "
            f"{summarize_milliseconds(metrics.combine_times)}"
        )
        if metrics.stream_seconds > 0:
            rate = metrics.total_stamps / metrics.stream_seconds
            print(
                f"stream wall time: {metrics.stream_seconds:.3f} s "
                f"({rate:.1f} stamps/s delivered)"
            )

        self._report_combined_offset()

    def _report_combined_offset(self) -> None:
        """Print the combined-offset summary for the run.

        Reports the frame the combine ran in (camera vs amplifier), the
        run-level offset with its uncertainty, how much the sensors
        disagreed, and the most recent per-acquisition offsets so the
        individual combined results are visible next to the aggregates.
        """
        valid = [c for c in self.combined_offsets if c.n_valid > 0]
        if not valid:
            print(
                "\n--- combined offset ---\n"
                "no acquisition had a valid sensor; nothing combined."
            )
            return

        amplifier_map = self.combiner.sensor_amplifiers
        camera_frame = bool(amplifier_map)
        dx = np.array([c.combined_dx for c in valid])
        dy = np.array([c.combined_dy for c in valid])
        error_dx = np.array([c.error_dx for c in valid])
        error_dy = np.array([c.error_dy for c in valid])
        scatter_dx = np.array([c.scatter_dx for c in valid])
        scatter_dy = np.array([c.scatter_dy for c in valid])
        n_valid = np.array([c.n_valid for c in valid])

        print(
            "\n--- combined offset (pixels) ---\n"
            f"combine frame        : "
            f"{'camera' if camera_frame else 'amplifier'}\n"
            f"acquisitions combined: {len(valid)}/"
            f"{len(self.combined_offsets)} with >=1 valid sensor\n"
            f"sensors per acq      : mean {n_valid.mean():.1f}, "
            f"min {n_valid.min()}, max {n_valid.max()}\n"
            f"overall dx           : {np.median(dx):+.3f} "
            f"+/- {np.nanmedian(error_dx):.3f} px "
            f"(rms over acqs {np.std(dx):.3f})\n"
            f"overall dy           : {np.median(dy):+.3f} "
            f"+/- {np.nanmedian(error_dy):.3f} px "
            f"(rms over acqs {np.std(dy):.3f})\n"
            f"sensor disagreement  : median scatter "
            f"dx {np.nanmedian(scatter_dx):.3f}, "
            f"dy {np.nanmedian(scatter_dy):.3f} px"
        )
        if camera_frame:
            amp_str = ", ".join(
                f"{name}:{amplifier_map[name]}" for name in sorted(amplifier_map)
            )
            print(f"per-sensor amplifier : {amp_str}")

        print("\nlast acquisitions (idx: dx +/- err, dy +/- err, sensors):")
        for offset in valid[-5:]:
            print(
                f"  {offset.stamp_index:6d}: "
                f"dx={offset.combined_dx:+.3f} +/- {offset.error_dx:.3f}  "
                f"dy={offset.combined_dy:+.3f} +/- {offset.error_dy:.3f}  "
                f"({offset.n_valid}/{offset.n_total})"
            )
