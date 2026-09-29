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
from .centroid_measurement import CentroidMeasurement
from .combined_offset import CombinedOffset
from .guider_tracker_config import GuiderTrackerConfig
from .guiding_status import GuidingStatus
from .measurement_state import MeasurementState
from .offset_combiner import OffsetCombiner
from .sensor_tracker import SensorTracker
from .streaming_metrics import StreamingMetrics

log = logging.getLogger(__name__)


class StreamingGuiderProcessor:
    """Per-stamp DAQ callback target for guide-star tracking.

    Each sensor has a ``SensorTracker`` that collects seed images, selects
    a reference, and measures subsequent stamps. If reference selection
    fails, it retries with all accumulated images after each new stamp.
    On success it returns measurements for every seed image. This processor
    pairs those results with the acquisition indices it retained.

    Each live acquisition is combined as soon as the
    next live ``stamp_index`` starts (see :meth:`_maybe_combine_previous`);
    pass ``on_combined_offset`` to be notified with each one as it is
    produced.
    :meth:`finalize` combines the seed warm-up frames and the final
    open frame once the stream stops.

    A change in ``metadata.sequence`` starts a new DAQ series and resets
    references, even if the ROI parameters repeat. The ``on_visit_*``
    names refer to these series, which can contain multiple images. An
    observation-label change alone does not reset references. Pass
    ``on_visit_start`` to be notified with ``(sequence, label)`` at the
    first stamp of each series, and ``on_visit_complete`` to be notified with
    ``(sequence, offsets)`` when each visit ends, where ``offsets`` are
    that series's combined offsets.

    Each ``on_stamp`` call must finish before the next begins.
    ``DaqStampSource`` already guarantees this.

    Within a DAQ series, stamps with the same ``stamp_index`` belong
    to the same acquisition. They arrive in acquisition order, with
    at most one stamp from each sensor for each index.

    When a sensor that already has a reference receives a stamp with
    a higher index, the processor combines the previous live acquisition
    using the measurements available so far. It does not wait for
    sensors that are still trying to select a reference.

    After a sensor selects its reference, it measures the seed images
    it stored earlier. These measurements can contribute to acquisitions
    that are still waiting to be combined. If an acquisition's result
    has already been emitted, it stays unchanged: a late measurement
    does not cause that result to be calculated and emitted again.

    DAQ sequence and stamp counters are unsigned 16-bit values. A change
    in sequence starts a new series, including rollover from 65535 to 0.
    Stamp indices are unwrapped within a series so indices beyond 65535
    remain distinct.

    Parameters
    ----------
    config : `GuiderTrackerConfig`
        Mutable settings shared with every sensor tracker. Updates affect
        subsequent processing without automatically resetting references.
    sensor_names : `dict` [`int`, `str`] or `None`, optional
        Detector names to use for specific DAQ sensor indices, for example
        ``{3: "R00_SG0"}``. For indices not in this mapping, the processor
        determines the guider detector name automatically.
        If an index belongs to a non-guider board, the processor uses a
        diagnostic label such as ``sensor_00``. Camera-coordinate conversion
        still requires a valid detector name from the camera model.
    progress_interval : `int`, optional
        Log progress every this many received stamps. Zero or a negative
        value disables progress logging.
    sensor_amplifiers : `dict` [`str`, `str`] or `None`, optional
        Amplifier name for each detector, for example
        ``{"R00_SG0": "C05"}``.
        If ``None``, the processor determines each sensor's amplifier
        from its first segment metadata in the DAQ series. It rebuilds
        this mapping when a new series starts.
        If you supply a dictionary, the processor uses that same
        dictionary without adding or replacing entries from DAQ metadata.
        An empty dictionary also disables automatic mapping.
        The processor converts offsets to camera coordinates, so every
        sensor contributing to a combined offset needs an entry.
    orientation : `GuiderOrientation` or `None`, optional
        Camera geometry used by the combiner. If omitted, the combiner
        creates an instance that loads the camera on its first transform.
    on_combined_offset : callable or `None`, optional
        Called with one ``CombinedOffset`` when a live acquisition closes.
        Combinations during series completion do not call this function.
    on_visit_start : callable or `None`, optional
        Called with ``(sequence, label)`` on the first stamp of a series.
    on_visit_complete : callable or `None`, optional
        Called with ``(sequence, offsets)`` when a series ends. ``offsets``
        is a new list of that series's results, sorted by acquisition index;
        the result objects are shared with ``combined_offsets``.

    Attributes
    ----------
    trackers : `dict` [`str`, `SensorTracker`]
        Trackers for the current series, keyed by detector name.
    seed_indices : `dict` [`str`, `list` [`int`]]
        Actual acquisition indices awaiting each sensor's first reference.
        Only trackers retain seed pixels; this processor retains indices.
    acquisitions : `dict` [`int`, `dict` [`str`, `CentroidMeasurement`]]
        Measurements awaiting combination, including rejected results.
    combiner : `OffsetCombiner`
        Combines accepted offsets in camera coordinates.
    combined_offsets : `list` [`CombinedOffset`]
        Results retained across all series. Each series is sorted when it
        completes; indices can repeat in different series.
    metrics : `StreamingMetrics`
        Counts and timing samples retained across all series.
    sensor_rois : `dict` [`str`, `tuple` [`int`, `int`, `int`]]
        Latest ``(segment, startrow, startcol)`` seen per sensor in this
        series. A change within a sequence produces a warning.
    visit_label : `str`
        First nonempty observation or series label seen in this series.


    Notes
    -----
    When used with ``DaqStampSource``, one C++ worker thread delivers
    all stamps and runs ``on_stamp`` and its callbacks sequentially.
    No new thread is created for individual callbacks.

    When ``finalize`` invokes a callback, that callback runs on the
    thread that called ``finalize``. In both cases, the callback must
    finish before the method continues.

    If a tracker, camera transformation, or callback raises an exception,
    the processor passes that exception to its caller. The stamp source
    should then be stopped and the run ended. Some internal state may
    have changed before the error, and the processor does not undo those
    changes. Continuing the run or retrying the failed call may therefore
    produce incorrect results.

    This processor is intended for development runs of limited duration.
    It keeps all combined offsets and timing samples in memory, so memory
    use grows as the run continues. Each sensor's tracker also stores
    incoming seed images until it selects a reference or the series ends.
    If reference selection keeps failing, that image buffer keeps growing.
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
        self.seed_indices: dict[str, list[int]] = {}
        self.acquisitions: dict[int, dict[str, CentroidMeasurement]] = {}
        amplifier_map = {} if sensor_amplifiers is None else sensor_amplifiers
        self.combiner = OffsetCombiner(amplifier_map, orientation)
        self.combined_offsets: list[CombinedOffset] = []
        self.metrics = StreamingMetrics()
        self.on_combined_offset = on_combined_offset
        self.on_visit_start = on_visit_start
        self.on_visit_complete = on_visit_complete
        self._visit_ended = False
        self._latest_stamp_index: int | None = None
        self._highest_stamp_index: int | None = None
        self._combined_indices: set[int] = set()
        self._current_sequence: int | None = None
        # Keep each sensor's ROI (segment, startrow, startcol) and the
        # image/series label for diagnostics. A change in metadata.sequence
        # starts a new series, even if the ROI and label stay the same.
        self.sensor_rois: dict[str, tuple[int, int, int]] = {}
        self.visit_label = ""
        # Starting list index for this visit's entries in combined_offsets.
        self._visit_start = 0
        # An explicitly supplied map is a fixed configuration and is
        # kept across visits; an auto-populated one is cleared on each
        # new visit so the new series repopulates it from its segments.
        self._amplifier_map_is_explicit = sensor_amplifiers is not None

    def set_sensor_amplifiers(self, sensor_amplifiers: dict[str, str]) -> None:
        """Replace the amplifier map and disable automatic mapping.

        Call this method before processing a DAQ series. The supplied
        map is kept across series and is not modified using stamp metadata.

        Parameters
        ----------
        sensor_amplifiers : `dict` [`str`, `str`]
            Mapping from detector names to ROI amplifier names, shared
            with the combiner. Every contributing detector needs an entry;
            a missing entry raises ``KeyError`` during combination.
            Passing an empty dictionary also disables automatic mapping.
        """
        self.combiner.sensor_amplifiers = sensor_amplifiers
        self._amplifier_map_is_explicit = True

    def _sensor_name(self, sensor_index: int) -> str:
        """Resolve a sensor name for a GDS ``sensor_index``.

        The stream carries only the packed index, so it is decoded to
        the detector name (e.g. ``R40_SG0``) - the key the orientation
        tables use.
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
        seen.

        The GDS segment defaults to 0 (``C00``) until the series
        ``start()`` caches the real value, so a stamp seen before its
        start (e.g. a subscriber joining mid-series) can record ``C00``.
        In practice ``start()`` precedes the sensor's stamps.
        """
        if (
            self._amplifier_map_is_explicit
            or sensor_name in self.combiner.sensor_amplifiers
        ):
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
        advances the sequence number, even if the ROI parameters stay
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

    @property
    def references(self) -> dict[str, CentroidMeasurement]:
        """Return current per-sensor references in a new dictionary.

        Values are the trackers' shared coordinate-only measurements.
        Their state remains ``NOT_SET``; sensors without a reference are
        omitted. Editing a value therefore also edits the tracker's reference.
        """
        return {
            name: tracker.reference
            for name, tracker in self.trackers.items()
            if tracker.reference is not None
        }

    def on_stamp(self, pixels: np.ndarray, metadata) -> None:
        """Process one sensor's DAQ stamp and update acquisition results.

        For each incoming stamp, this method identifies the sensor and
        sends the image to its ``SensorTracker``. It records measurements
        by acquisition so they can be combined with those from other
        sensors. Combined offsets are reported through
        ``on_combined_offset`` as live acquisitions finish.

        The tracker collects seed images until it selects a reference.
        When selection succeeds, it measures those stored images. This
        method then records that batch of measurements under their
        original acquisition indices. Subsequent stamps each produce
        one measurement.

        A change in DAQ sequence finishes the previous series and resets
        tracking for the new series. This method also records processing
        counts and timings.

        Parameters
        ----------
        pixels : `numpy.ndarray`
            Two-dimensional stamp. The tracker copies any seed pixels it
            retains, so the caller may reuse the array after this call.
        metadata : object
            DAQ metadata with ``sensor_index`` and ``stamp_index``.
            ``sequence`` identifies a series (defaults to zero). ``segment``
            supplies the automatic amplifier map; ``startrow``, ``startcol``,
            ``obs_id``, and ``series_id`` provide ROI and label diagnostics.

        Raises
        ------
        RuntimeError
            If a tracker returns measurements, timings, and acquisition
            indices whose lengths disagree.

        Notes
        -----
        Input must be serialized and ordered by acquisition within a series.
        Tracker, transform, and callback errors propagate to the caller.
        Stamps of a successfully finalized sequence are ignored but still
        counted and timed; a different sequence starts a new series.
        """
        callback_start = perf_counter()
        self.metrics.total_stamps += 1
        if self.metrics.first_stamp_time is None:
            self.metrics.first_stamp_time = callback_start

        try:
            visit_started = self._maybe_start_new_visit(
                int(getattr(metadata, "sequence", 0))
            )
            if self._visit_ended:
                return

            sensor_name = self._sensor_name(int(metadata.sensor_index))
            stamp_index = self._unwrap_stamp_index(int(metadata.stamp_index))
            self._record_amplifier(sensor_name, metadata)
            self._record_roi(sensor_name, metadata)
            if visit_started and self.on_visit_start is not None:
                self.on_visit_start(self._current_sequence, self.visit_label)
            tracker = self.trackers.get(sensor_name)
            if tracker is None:
                tracker = SensorTracker(sensor_name, self.config)
                self.trackers[sensor_name] = tracker
                self.seed_indices[sensor_name] = []

            seeding = tracker.reference is None
            if seeding:
                self.metrics.seed_stamps += 1
                self.seed_indices[sensor_name].append(stamp_index)

            # The tracker owns copied seed images, expanding-prefix retries,
            # and replay. Retain actual indices: gaps and rollover mean they
            # cannot be reconstructed from the returned batch length.
            was_error = tracker.get_status() == GuidingStatus.ERROR
            try:
                tracker.process_stamp(pixels)
            finally:
                # A tracker already in ERROR refuses processing before
                # clearing its previous timing. Do not count that again.
                if not was_error and tracker.lock_duration is not None:
                    self.metrics.lock_times.append(tracker.lock_duration)

            self._record_tracker_measurements(
                sensor_name, tracker, stamp_index, seeding
            )
        finally:
            end = perf_counter()
            self.metrics.callback_times.append(end - callback_start)
            self.metrics.last_stamp_time = end

        # Do not let progress reporting replace a processing exception.
        if (
            self.progress_interval > 0
            and self.metrics.total_stamps % self.progress_interval == 0
        ):
            self._log_progress()

    def _record_tracker_measurements(
        self,
        sensor_name: str,
        tracker: SensorTracker,
        stamp_index: int,
        seeding: bool,
    ) -> None:
        """Record the latest tracker measurements and their processing times.

        ``seeding`` means the tracker had no reference before the latest
        ``process_stamp`` call. In that case, match any measurements to
        the stored seed indices. Otherwise, use the current ``stamp_index``.
        """
        measurements = tracker.get_measurements()
        durations = tracker.measurement_durations
        if len(measurements) != len(durations):
            raise RuntimeError(
                f"Sensor {sensor_name}: measurement and timing counts differ."
            )
        if tracker.reference is None:
            if measurements:
                raise RuntimeError(
                    f"Sensor {sensor_name}: measurements without a reference."
                )
            return

        indices = self.seed_indices[sensor_name] if seeding else [stamp_index]
        if len(indices) != len(measurements):
            raise RuntimeError(
                f"Sensor {sensor_name}: acquisition and measurement counts differ."
            )
        self.metrics.measure_times.extend(durations)
        self.metrics.valid_measurements += sum(
            m.state == MeasurementState.PASSED_QUALITY for m in measurements
        )
        self.metrics.invalid_measurements += sum(
            m.state != MeasurementState.PASSED_QUALITY for m in measurements
        )
        if seeding:
            del self.seed_indices[sensor_name]
        for index, measurement, duration in zip(
            indices, measurements, durations, strict=True
        ):
            self._store_measurement(
                sensor_name, tracker, index, measurement, duration, seeding
            )

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
        """Log lifetime counts and the rate of received stamps."""
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

    def _store_measurement(
        self,
        sensor_name: str,
        tracker: SensorTracker,
        stamp_index: int,
        measurement: CentroidMeasurement,
        measurement_duration: float,
        seed_replay: bool,
    ) -> None:
        """Store an already-measured result under its acquisition index.

        ``seed_replay`` is true for every result from the first successful
        lock, including the stamp that completed locking. Those results do
        not advance live closure. Counts and ``measurement_duration`` have
        already been recorded, including any result discarded here.
        """
        if seed_replay and stamp_index in self._combined_indices:
            # Another sensor may have advanced the live stream while this
            # sensor was still seeding. Do not reopen an emitted result.
            self.metrics.discarded_seed_measurements += 1
            return
        self.acquisitions.setdefault(stamp_index, {})[sensor_name] = measurement
        # Only stamps from an already-locked sensor drive the live combine.
        # Seed-replay frames arrive in bulk, out of global stamp order
        # as each sensor locks, so combining
        # on them would emit single-sensor offsets and drop the frames
        # of later-locking sensors; finalize() combines them instead.
        if not seed_replay:
            self._maybe_combine_previous(stamp_index)

        if log.isEnabledFor(logging.DEBUG):
            # The per-sensor offset from the locked reference comes from
            # the returned result, without another fit.
            # Rejected results can contain NaN coordinates; diagnostic
            # subtraction must not call the finite-offset validation API.
            offset_x = measurement.x - tracker.reference.x
            offset_y = measurement.y - tracker.reference.y
            log.debug(
                "%s idx=%d dx=%+.3f dy=%+.3f snr=%.1f fwhm=%.2f %s "
                "(measure %.2f ms)",
                sensor_name,
                stamp_index,
                offset_x,
                offset_y,
                measurement.snr,
                measurement.fwhm,
                (
                    "ok"
                    if measurement.state == MeasurementState.PASSED_QUALITY
                    else "reject"
                ),
                measurement_duration * 1e3,
            )

    def _maybe_combine_previous(self, stamp_index: int) -> None:
        """Combine the previous acquisition once a newer live one starts.

        Called only for live stamps (see :meth:`_store_measurement`).
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

    def _maybe_start_new_visit(self, sequence: int) -> bool:
        """Detect a new guide series and re-acquire.

        The sequence advances on every START (per Gregg, 2026-07-06),
        so a change means a new series: typically a slew to a new field
        with new guide stars, but possibly a restart with the same ROI
        parameters. Both cases require the reset - ``stamp_index``
        restarts each series, and after a slew the old references point
        at stars that are no longer there. The comparison is by
        inequality, so the 16-bit sequence rollover is harmless. The
        very first stamp only records the starting sequence.

        Returns
        -------
        visit_started : `bool`
            True for the first sequence or a change of sequence; False
            for another stamp in the current sequence.
        """
        if self._current_sequence is None:
            self._current_sequence = sequence
            return True
        if sequence == self._current_sequence:
            return False
        self._end_visit()
        self._reset_for_new_visit(sequence)
        return True

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
        visit_start = self._visit_start
        visit_offsets = self.combined_offsets[visit_start:]
        visit_offsets.sort(key=lambda offset: offset.stamp_index)
        self.combined_offsets[visit_start:] = visit_offsets
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
        for tracker in self.trackers.values():
            if tracker.reference is None:
                tracker.reset()
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
        self.seed_indices.clear()
        self.acquisitions.clear()
        self._latest_stamp_index = None
        self._highest_stamp_index = None
        self._combined_indices.clear()
        self._visit_start = len(self.combined_offsets)
        self._current_sequence = sequence
        self.sensor_rois.clear()
        self.visit_label = ""
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

        Tracker, camera, and completion-callback errors propagate. Repeated
        calls are harmless only after successful finalization; partial work
        from a failed finalization is not rolled back or retried.
        """
        self._end_visit()
