.. py:currentmodule:: lsst.ts.guider.pipeline

.. _streaming-guider-pipeline:

Live guider processing
========================================

``StreamingGuiderProcessor`` is the stamp-callback adapter shared by the
live DAQ demo and the later CSC integration.
One ``SensorTracker`` per sensor warms up, locks a reference, and then
measures incoming stamps. ``OffsetCombiner`` combines each acquisition
using the same camera transforms as the :ref:`offline runner <offset-combination>`.

The processing path is::

    GDS START / RESUME / STAMP
        -> C++ Decoder caches ROI and image metadata
        -> guiderGDS callback copies pixels into a NumPy array
        -> StreamingGuiderProcessor buffers seeds or measures the star
        -> OffsetCombiner produces a CombinedOffset
        -> on_combined_offset / on_visit_complete

The C++ worker serializes callbacks across all subscribed sensors.
Direct callers must also serialize calls and stop/join the source before
calling ``finalize``. User callbacks run on that same worker thread;
they should return promptly. Handing results to an asyncio event loop
and publishing SAL topics are responsibilities of the subsequent CSC stage.

Metadata and identity
----------------------------------------

``StampMetadata`` carries the following data across the C++/Python boundary:

.. list-table:: DAQ metadata
   :header-rows: 1
   :widths: 25 75

   * - Field
     - Meaning
   * - ``sensor_index``
     - Packed GDS location, decoded to a camera name such as ``R40_SG0``.
   * - ``sensor_name``
     - SDK location string for diagnostics; distinct from the camera name.
   * - ``sequence``
     - Unsigned 16-bit START counter, identifying a guide series.
   * - ``stamp_index``
     - Unsigned 16-bit acquisition counter from ``StateMetadata.stamp()``.
   * - ``timestamp_ns``
     - DAQ timestamp in TAI nanoseconds.
   * - ``segment``, ``startrow``, ``startcol``
     - Per-sensor ROI amplifier and window origin, cached on START.
   * - ``series_id``
     - ROI/configuration identity from ``SeriesMetadata.id()``, cached on START.
   * - ``obs_id``
     - Image name from the RESUME comment; START clears the previous value.

The decoder caches ROI and image values independently for each sensor.
One ROI series can contain multiple images: a RESUME comment changes
``obs_id`` without changing the series identity.
The processor resets references when the START sequence changes; a different
image label alone does not reset them. ``visit_label`` is the first
available image/series label used for that series' reporting.
A changed ROI within the same sequence is logged as a warning.

The processor decodes ``sensor_index`` using :ref:`sensor-identifiers`.
An explicit ``sensor_names`` map overrides that decoding.
The amplifier map is populated from each sensor's first ``segment``.
Automatic entries are cleared for a new series; explicitly configured
entries are retained. Complete valid metadata is needed for camera-frame
offsets, as described under :ref:`offset-combination`.

Seeding and acquisition completion
----------------------------------------

The DAQ guarantees ordered delivery without repeated sensor/stamp pairs.
The processor relies on that contract and does not keep a duplicate-arrival
history or reject older sequence numbers.

Each sensor buffers ``GuiderTrackerConfig.seed_frames`` stamps.
The processor owns copies of buffered pixels so a caller can reuse its
image buffer safely. The full seed cube is passed to ``lock_reference``
for candidate validation over that window.
Successful locking replays the buffered stamps through the ordinary
measurement path. A failed attempt discards that window and retries
with the next full window.

Measurements are grouped by acquisition index. The original completion
rule is retained: the arrival of a higher-index live measurement closes
the previous live acquisition. This assumes sensors normally deliver
an acquisition in a burst before the next one. There is no expected-sensor
count or wall-clock timeout in this processor.

* Sensor order within a pending acquisition can vary.
* A missing sensor is omitted when the acquisition closes. ``n_total``
  counts measured sensors, and ``n_valid`` counts accepted offsets.
* Seed replay does not advance the live completion trigger: sensors can
  lock at different times and replay frames out of global order.
  Pending warm-up acquisitions are combined at the series boundary.
  Replay for an acquisition already combined is discarded so it cannot
  reopen or revise the result. This is internal replay of buffered data.
* Acquisition indices with no measurements produce no result.

This policy favors prompt results over waiting indefinitely for every
sensor. A sensor that has not yet locked can therefore be absent from an
already-published result even if it later locks successfully.
``discarded_seed_measurements`` counts buffered measurements omitted
because their acquisition was already combined.

Series transitions and shutdown
----------------------------------------

A change in sequence ends the previous series, combines its pending
acquisitions, and clears trackers, references, seed buffers, ROI records,
and acquisition bookkeeping. Even an unchanged ROI requires this reset:
a new START may follow a telescope slew, and the acquisition counter
restarts. Lifetime metrics and the accumulated results are retained.

The original sequence comparison is retained: a different value starts
a new series, including rollover from 65535 to 0.
Within a series, acquisition indices are unwrapped so a raw stamp counter
of 0 after 65535 is represented as 65536 in ``CombinedOffset.stamp_index``.
The original raw value remains in ``StampMetadata``. With ordered incoming
stamps, a decrease in that raw counter indicates rollover.

``on_visit_start(sequence, label)`` is called once at the first accepted
stamp. ``on_combined_offset(result)`` reports acquisitions completed live.
``on_visit_complete(sequence, offsets)`` reports all results for that
series, sorted by acquisition index, including pending warm-up and final
frames. Cleanup frames do not additionally fire ``on_combined_offset``.
The completion callback also reports an empty list for a series that
never locked a reference.

Stop the source first, then call ``finalize``. Finalization ends the last
series and is safe to repeat without repeating results or notifications.
A partial seed window is discarded without attempting a shorter lock.
Subsequent stamps from the finalized series are ignored; a newer sequence
can start another series on the same processor.

Metrics and reporting
----------------------------------------

``StreamingMetrics`` retains counts and timings in seconds:

* Total arrivals, seed stamps, accepted/rejected measurements.
* Seed measurements discarded because their acquisition already closed.
* Reference-lock, individual measurement, callback, and combination times.
* First/last callback times for the delivered stream's wall duration.

Combination timing measures only the combiner call. Callback timing
includes work from callback entry to exit; it is not latency from the
DAQ acquisition timestamp. The ``--per-frame`` demo labels the printed
value as ``combine`` time.
Timing samples and combined results are retained for the processor's
lifetime, so memory grows with the run. Recreate the processor between
bounded runs if retaining the complete history is unnecessary.

Console formatting lives in ``demo/streaming_report.py`` and the demo's
logging callbacks. The processor exposes results and metrics independently
of that presentation.

Validation and extraction notes
----------------------------------------

The streaming tests cover seed replay, failed locks, short series,
missing stamps, staggered locking, sequence and
stamp rollover, repeated finalization, metadata refresh, and agreement
with the offline algorithm using real centroiding and camera geometry.
The :ref:`DAQ demo guide <daq-guider-pipeline-demo>` describes the emulator
test through the actual C++ decoder and worker callback.

The implementation comes from OSW-2858's ``pipeline/streaming.py``.
``StreamingGuiderProcessor`` and ``StreamingMetrics`` have matching module
names; printing helpers move into ``demo/``. The original reference and
combination algorithms and live-completion rule are retained.
Explicit corrections prevent buffered seed replay from reopening emitted
results, counter collisions at rollover, and repeated completion
notifications. Buffered float32 images are copied, and image labels are
recorded even when optional ROI metadata is absent. Incoming stamps rely
on the DAQ's ordered, nonduplicated delivery contract.

API reference
----------------------------------------

``StreamingGuiderProcessor`` and ``StreamingMetrics`` are included in the
pipeline API reference under :ref:`sensor-tracking`.
