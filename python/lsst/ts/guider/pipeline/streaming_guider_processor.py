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

__all__ = ["StreamingGuiderProcessor"]

import logging
from collections.abc import Callable
from time import perf_counter

import numpy as np

from .. import sensor_identifiers
from ..guider_orientation import GuiderOrientation
from .combined_offset import CombinedOffset
from .guider_tracker_config import GuiderTrackerConfig
from .offset_combiner import OffsetCombiner
from .sensor_tracker import SensorTracker
from .streaming_metrics import StreamingMetrics

log = logging.getLogger(__name__)


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

    Callbacks must be serialized, as they are by ``DaqStampSource``.
    DAQ stamps are expected in acquisition order, without repeated
    sensor/stamp pairs. A newer live stamp closes the previous live
    acquisition using the measurements available then. Buffered seed
    replay can revisit earlier acquisitions as sensors finish locking;
    it must not reopen a result that has already been combined.

    DAQ sequence and stamp counters are unsigned 16-bit values. A change
    in sequence starts a new series, including rollover from 65535 to 0.
    Stamp indices are unwrapped within a series so indices beyond 65535
    remain distinct.
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
        self._visit_ended = False
        self._latest_stamp_index: int | None = None
        self._highest_stamp_index: int | None = None
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
            return sensor_identifiers.detector_name_from_sensor_index(sensor_index)
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
            sensor_identifiers.amplifier_name_from_segment_index(int(segment))
        )

    def _record_roi(self, sensor_name: str, metadata) -> None:
        """Record the sensor's ROI key and visit label, for diagnostics.

        A change in the sequence number marks a new visit. Each START
        advances the sequence number, even when the ROI parameters stay
        the same.

        The ROI key is logged when each sensor first appears in a visit.
        A change to that sensor's ROI within the same sequence is
        unexpected under the DAQ protocol and is logged as a warning.
        """
        if not self.visit_label:
            self.visit_label = str(
                getattr(metadata, "obs_id", "") or getattr(metadata, "series_id", "")
            )
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
        """DAQ callback for ordered stamps. Runs on the C++ worker thread."""
        callback_start = perf_counter()
        self.metrics.total_stamps += 1
        if self.metrics.first_stamp_time is None:
            self.metrics.first_stamp_time = callback_start

        try:
            self._maybe_start_new_visit(int(getattr(metadata, "sequence", 0)))
            if self._visit_ended:
                return

            sensor_name = self._sensor_name(int(metadata.sensor_index))
            stamp_index = self._unwrap_stamp_index(int(metadata.stamp_index))
            self._record_amplifier(sensor_name, metadata)
            self._record_roi(sensor_name, metadata)
            self._maybe_notify_visit_start()
            stamp = np.asarray(pixels, dtype=np.float32)

            tracker = self.trackers.get(sensor_name)
            if tracker is None:
                tracker = SensorTracker(sensor_name, self.config)
                self.trackers[sensor_name] = tracker
                self.seed_buffers[sensor_name] = []
                self.seed_indices[sensor_name] = []

            if tracker.reference_center is None:
                self.metrics.seed_stamps += 1
                # The callback owns pixels only for this call. Retain a copy
                # even when a direct caller supplies an already-float32 array.
                self.seed_buffers[sensor_name].append(stamp.copy())
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
        finally:
            end = perf_counter()
            self.metrics.callback_times.append(end - callback_start)
            self.metrics.last_stamp_time = end

            if (
                self.progress_interval > 0
                and self.metrics.total_stamps % self.progress_interval == 0
            ):
                self._log_progress()

    def _unwrap_stamp_index(self, stamp_index: int) -> int:
        """Extend the ordered 16-bit wire counter across rollover."""
        highest = self._highest_stamp_index
        if highest is not None:
            stamp_index += highest - highest % 65536
            # Under ordered delivery, a lower raw counter means rollover.
            if stamp_index < highest:
                stamp_index += 65536
        self._highest_stamp_index = stamp_index
        return stamp_index

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
        described by ``SensorTracker.lock_reference``. Once locked, the
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
        if receipt_time is None and stamp_index in self._combined_indices:
            # Another sensor may have advanced the live stream while this
            # sensor was still seeding. Do not reopen an emitted result.
            self.metrics.discarded_seed_measurements += 1
            return
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

        Seed replay for an already-combined index is discarded before
        storing measurements. ``_combined_indices`` also guards against
        combining an acquisition twice.
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
        if self._current_sequence is None or self._visit_ended:
            return
        self._visit_ended = True
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
        # A short or unsuccessful series still has a completion boundary.
        # Mark it ended before notifying so repeated finalization is harmless.
        self.seed_buffers.clear()
        self.seed_indices.clear()
        if self.on_visit_complete is not None:
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
        self._highest_stamp_index = None
        self._combined_indices.clear()
        self._visit_start = len(self.combined_offsets)
        self._current_sequence = sequence
        self.sensor_rois.clear()
        self.visit_label = ""
        self._visit_start_notified = False
        self._visit_ended = False
        if not self._amplifier_map_is_explicit:
            self.combiner.sensor_amplifiers = {}

    def finalize(self) -> None:
        """End the final visit once the stream stops.

        Equivalent to a sequence boundary with no next series: the last
        visit's trailing acquisitions (seed warm-up frames and the final
        open frame) are combined and ``on_visit_complete`` fires for it.
        Stop the source and join its worker before calling this method.
        Repeated calls do not repeat results or completion notifications.
        A partial seed window is discarded without attempting to lock.
        Later stamps from this same series are ignored; a newer sequence
        can start a new series on this processor.
        """
        self._end_visit()
