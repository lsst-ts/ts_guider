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

Rubin observes one field, then slews to the next; each pointing is a
separate DAQ series carrying its own ``metadata.sequence`` and its own
guide stars. When the sequence changes the tracked stars are no longer
valid, so the processor ends the finished visit (combining its last
open acquisition) and resets its per-visit state - trackers,
references, seed buffers and per-``stamp_index`` bookkeeping - so the
new field re-seeds and re-locks from scratch. ``stamp_index`` restarts
each series, so this reset is also what keeps the live combine trigger
and the duplicate guard correct across the boundary.
"""

from __future__ import annotations

__all__ = ["StreamingMetrics", "StreamingGuiderProcessor"]

import logging
import sys
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
    """Per-stamp DAQ callback target for guide-star tracking.

    One ``SensorTracker`` per ``sensor_index``. Warms up, locks, then
    measures. Each locked acquisition is combined live, as soon as the
    next live ``stamp_index`` starts (see :meth:`_maybe_combine_previous`);
    pass ``on_combined_offset`` to be notified with each one as it is
    produced (e.g. to publish telemetry or print a live trace).
    :meth:`finalize` combines the seed warm-up frames and the final
    open frame once the stream stops.

    A change in ``metadata.sequence`` marks a new pointing; the finished
    visit is ended and the per-visit state reset so the new field
    re-acquires (see :meth:`_maybe_start_new_visit`). Pass
    ``on_visit_start`` to be notified with ``(sequence, label)`` at the
    first stamp of each visit (e.g. to print a banner grouping the live
    trace), and ``on_visit_complete`` to be notified with
    ``(sequence, offsets)`` when each visit ends, where ``offsets`` are
    that visit's combined offsets (e.g. to publish a per-visit summary).
    """

    def __init__(
        self,
        config: GuiderTrackerConfig,
        sensor_names: dict[int, str] | None = None,
        progress_interval: int = 0,
        sensor_amplifiers: dict[str, str] | None = None,
        orientation: GuiderOrientation | None = None,
        on_combined_offset: Callable[[CombinedOffset], None] | None = None,
        on_visit_start: Callable[[int | None, str], None] | None = None,
        on_visit_complete: (
            Callable[[int | None, list[CombinedOffset]], None] | None
        ) = None,
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
        self.on_visit_start = on_visit_start
        self.on_visit_complete = on_visit_complete
        self._visit_start_notified = False
        self._latest_stamp_index: int | None = None
        self._combined_indices: set[int] = set()
        self._current_sequence: int | None = None
        # Per-visit diagnostics, not the boundary trigger: each sensor's
        # ROI key (segment, startrow, startcol) and the image/series
        # label from the stamp metadata. The sequence is the trigger; a
        # new START always advances it (per Gregg slack comment, 2026-07-06),
        # even when the ROI parameters repeat.
        self.sensor_rois: dict[str, tuple[int, int, int]] = {}
        self.visit_label = ""
        # Index into combined_offsets where the current visit's offsets
        # begin, so a visit can be summarized without a separate list.
        self._visit_start = 0
        # An explicitly supplied map is a fixed configuration and is
        # kept across visits; an auto-populated one is cleared on each
        # new visit so the new series repopulates it from its segments.
        self._amplifier_map_is_explicit = sensor_amplifiers is not None

    def set_sensor_amplifiers(self, sensor_amplifiers: dict[str, str]) -> None:
        """Set the per-sensor amplifier map used for the camera combine.

        Populated from the ROI configuration (segment per sensor). With
        it the per-acquisition combine runs in the camera frame; without
        it the combine stays in amplifier coordinates. A map set here is
        treated as explicit configuration and kept across visits.
        """
        self.combiner.sensor_amplifiers = sensor_amplifiers
        self._amplifier_map_is_explicit = True

    def _sensor_name(self, sensor_index: int) -> str:
        """Resolve a sensor name for a GDS ``sensor_index``.

        The stream carries only the packed index, so it is decoded to
        the detector name (e.g. ``R40_SG0``) - the key the orientation
        tables use. This makes per-sensor names meaningful in the logs
        and lets the camera-frame transform resolve once the per-sensor
        amplifier map is provided (the camera combine still needs that
        map). An explicit name map, if supplied, takes precedence.
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

    def _record_roi(self, sensor_name: str, metadata) -> None:
        """Record the sensor's ROI key and visit label, for diagnostics.

        The visit boundary is triggered by the sequence, not by these:
        a new START always advances the sequence, even when it repeats
        the same ROI parameters. The ROI key is logged once per sensor
        per visit; a mid-visit change without a sequence change should
        be impossible, so it is logged as a warning if it ever happens.
        """
        segment = getattr(metadata, "segment", None)
        startrow = getattr(metadata, "startrow", None)
        startcol = getattr(metadata, "startcol", None)
        if segment is None or startrow is None or startcol is None:
            return
        roi_key = (int(segment), int(startrow), int(startcol))
        known = self.sensor_rois.get(sensor_name)
        if known is None:
            self.sensor_rois[sensor_name] = roi_key
            log.info(
                "%s ROI: segment=%d startrow=%d startcol=%d",
                sensor_name,
                *roi_key,
            )
        elif known != roi_key:
            log.warning(
                "%s ROI changed mid-visit without a sequence change: "
                "%s -> %s. Boundary may have been missed.",
                sensor_name,
                known,
                roi_key,
            )
            self.sensor_rois[sensor_name] = roi_key

        if not self.visit_label:
            self.visit_label = str(
                getattr(metadata, "obs_id", "") or getattr(metadata, "series_id", "")
            )

    def _maybe_notify_visit_start(self) -> None:
        """Fire ``on_visit_start`` once, at the first stamp of a visit.

        Called after :meth:`_record_roi` so the image/series label is
        available. Firing here rather than at the sequence boundary
        means the first visit (which has no preceding boundary) is
        announced too, and the label is populated by the time it fires.
        """
        if self._visit_start_notified:
            return
        self._visit_start_notified = True
        if self.on_visit_start is not None:
            self.on_visit_start(self._current_sequence, self.visit_label)

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

        self._maybe_start_new_visit(int(getattr(metadata, "sequence", 0)))

        sensor_name = self._sensor_name(int(metadata.sensor_index))
        self._record_amplifier(sensor_name, metadata)
        self._record_roi(sensor_name, metadata)
        self._maybe_notify_visit_start()
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

    def _maybe_start_new_visit(self, sequence: int) -> None:
        """Detect a new guide series and re-acquire.

        The sequence advances on every START (per Gregg, 2026-07-06),
        so a change means a new series: typically a slew to a new field
        with new guide stars, but possibly a restart with the same ROI
        parameters. Both cases require the reset - ``stamp_index``
        restarts each series, and after a slew the old references point
        at stars that are no longer there. The comparison is by
        inequality, so the 16-bit sequence rollover is harmless. The
        very first stamp only records the starting sequence.
        """
        if self._current_sequence is None:
            self._current_sequence = sequence
            return
        if sequence == self._current_sequence:
            return
        self._end_visit()
        self._reset_for_new_visit(sequence)

    def _end_visit(self) -> None:
        """Combine the finishing visit's pending acquisitions and notify.

        The trailing acquisitions (seed warm-up frames, replayed in bulk
        at lock time and hence out of global order, plus the final open
        frame) are combined and the visit's slice of ``combined_offsets``
        is ordered by ``stamp_index``. ``on_visit_complete`` is then
        called with that visit's offsets. Trailing combines do not fire
        ``on_combined_offset`` (that reports frames completed live).
        """
        for stamp_index in sorted(self.acquisitions):
            self._combine_acquisition(stamp_index, notify=False)
        visit_offsets = self.combined_offsets[self._visit_start :]
        visit_offsets.sort(key=lambda offset: offset.stamp_index)
        self.combined_offsets[self._visit_start :] = visit_offsets
        log.info(
            "Visit complete: sequence=%s image='%s' "
            "%d combined offsets from %d sensors.",
            self._current_sequence,
            self.visit_label,
            len(visit_offsets),
            len(self.sensor_rois),
        )
        if self.on_visit_complete is not None and visit_offsets:
            self.on_visit_complete(self._current_sequence, visit_offsets)

    def _reset_for_new_visit(self, sequence: int) -> None:
        """Drop the finished visit's tracking state before re-acquiring.

        ``combined_offsets`` and the lifetime metrics are kept (the run
        report aggregates across visits); everything tied to the old
        field's stars is cleared. Resetting ``_latest_stamp_index`` and
        ``_combined_indices`` is required for correctness, not just
        tidiness: ``stamp_index`` restarts each series, so without it the
        new visit's low indices would never re-trigger the live combine
        and would be skipped by the duplicate guard.
        """
        self.trackers.clear()
        self.seed_buffers.clear()
        self.seed_indices.clear()
        self.acquisitions.clear()
        self._latest_stamp_index = None
        self._combined_indices.clear()
        self._visit_start = len(self.combined_offsets)
        self._current_sequence = sequence
        self.sensor_rois.clear()
        self.visit_label = ""
        self._visit_start_notified = False
        if not self._amplifier_map_is_explicit:
            self.combiner.sensor_amplifiers = {}

    def finalize(self) -> None:
        """End the final visit once the stream stops.

        Equivalent to a sequence boundary with no next series: the last
        visit's trailing acquisitions (seed warm-up frames and the final
        open frame) are combined and ``on_visit_complete`` fires for it.
        The stream can stop on a rough shutdown, so flush stdout to keep
        any callback output that the block buffer has not written yet.
        """
        self._end_visit()
        sys.stdout.flush()

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
        sys.stdout.flush()

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
