.. py:currentmodule:: lsst.ts.guider.pipeline

.. _streaming-guider-pipeline:

Live guider processing
========================================

``StreamingGuiderProcessor`` receives DAQ stamps, passes each image to
the appropriate ``SensorTracker``, and groups the returned measurements
so ``OffsetCombiner`` can calculate a camera offset for each acquisition.
It uses the tracking and camera transformations described in
:ref:`sensor-tracking` and :ref:`offset-combination`.

Three terms distinguish the data being processed:

* A **stamp** is one sensor's ROI image, delivered to ``on_stamp`` with
  its metadata.
* An **acquisition** groups stamps from different sensors with the same
  ``stamp_index`` within a DAQ series. Up to eight guide sensors can
  contribute to one combined offset.
* A **DAQ series** is identified by ``sequence``. A change in sequence
  ends the current series and resets tracking. The ``on_visit_*`` callback
  names refer to these series, which can contain multiple images.

The processor is used by the standalone live validation demo. It exposes
offsets and processing metrics; it does not publish SAL topics or send
telescope corrections.

Main call flow
----------------------------------------

The following trees show the main method calls. Indentation means that
one method calls another; calls at the same level run from top to bottom.
Metadata and timing helpers are omitted. Optional callbacks run only
when supplied by the caller.

During streaming, each stamp follows this path on the DAQ worker thread:

.. code-block:: text

   DAQ worker callback
   `-- StreamingGuiderProcessor.on_stamp(pixels, metadata)
       |-- _maybe_start_new_visit(sequence)
       |   `-- [changed sequence] _end_visit(); _reset_for_new_visit(...)
       |-- on_visit_start(sequence, label)  [first stamp of a series]
       |-- SensorTracker.process_stamp(pixels)
       |   |-- _try_lock_reference(...)  [enough seeds, no reference yet]
       |   `-- measure(...)  [seed replay after locking, or one live stamp]
       `-- _record_tracker_measurements(...)
           `-- _store_measurement(...)  [for each returned measurement]
               `-- _maybe_combine_previous(...)  [live measurements only]
                   `-- _combine_acquisition(..., notify=True)
                       |-- OffsetCombiner.combine(...)
                       `-- on_combined_offset(result)

``_maybe_combine_previous`` calls the combiner only when a higher live
acquisition index arrives. Seed replay stores measurements for pending
acquisitions without advancing that live completion rule.

After the source has stopped and its worker has joined, the caller ends
the last series through this path:

.. code-block:: text

   StreamingGuiderProcessor.finalize()
   `-- _end_visit()
       |-- _combine_acquisition(..., notify=False)  [each pending index]
       |   `-- OffsetCombiner.combine(...)
       `-- on_visit_complete(sequence, offsets)

The same ``_end_visit`` path runs when the sequence changes during
streaming. It combines pending acquisitions without calling
``on_combined_offset``, then reports the complete series through
``on_visit_complete``.

Reference selection and measurements
----------------------------------------

For every stamp, ``on_stamp`` reads the metadata, obtains the sensor's
tracker, and calls ``SensorTracker.process_stamp``. The tracker owns
reference selection, image measurements, and copies of any images it
needs to retain. The processor keeps the corresponding acquisition
indices so it can associate returned measurements with their inputs.

Each tracker first collects ``config.seed_frames`` images. It then tries
to select a reference star using those images. If no suitable reference
is found, it retains them and retries after every additional image.
For example, with ``seed_frames=10``, attempts use the first 10, then 11,
then 12 images, until selection succeeds or the series ends.

When a reference is selected, the tracker measures all stored seed
images and releases them. This is **seed replay**: several measurements
become available during one ``on_stamp`` call, each belonging to an
earlier input or to the stamp that completed reference selection.
The processor records each result under its original acquisition index,
including when indices contain gaps or cross the counter rollover.
After that, each incoming stamp produces one measurement.

Reference selection and measurement quality are separate. A rejected
measurement leaves the tracker locked to its existing reference. Only
measurements with ``state == MeasurementState.PASSED_QUALITY`` contribute
to the combined offset. Rejected results remain available in the
acquisition's diagnostics. References hold fixed coordinates in
``CentroidMeasurement`` objects whose state remains ``NOT_SET``; that
state is not used to accept or reject an offset.

All trackers share the processor's mutable configuration. Changing it
affects subsequent processing without resetting existing references.

DAQ metadata
----------------------------------------

``StampMetadata`` carries the sensor identity, acquisition counters, and
ROI information across the C++/Python boundary:

.. list-table:: Metadata used by the streaming path
   :header-rows: 1
   :widths: 30 70

   * - Field
     - Meaning
   * - ``sensor_index``
     - Packed GDS location, decoded to a detector name such as ``R40_SG0``.
   * - ``sensor_name``
     - SDK location string for diagnostics, distinct from the detector name.
   * - ``sequence``
     - Unsigned 16-bit START counter identifying the DAQ series.
   * - ``stamp_index``
     - Unsigned 16-bit acquisition counter shared by participating sensors.
   * - ``timestamp_ns``
     - DAQ timestamp in TAI nanoseconds. Acquisition grouping uses the counters.
   * - ``segment``, ``startrow``, ``startcol``
     - Per-sensor ROI amplifier and window origin, cached by the decoder on START.
   * - ``series_id``
     - Series label from ``SeriesMetadata.id()``, cached on START.
   * - ``obs_id``
     - Image label from the RESUME comment; START clears the previous value.

The decoder keeps the ROI and image labels for each sensor. A new RESUME
can change ``obs_id`` within the same series. The processor keeps its
references until ``sequence`` changes, even if the image label changes.
An ROI change within the same sequence produces a warning; it does not
reset tracking or replace an automatically recorded amplifier.

For reporting, ``visit_label`` takes the first nonempty label seen in
the series, preferring ``obs_id`` over ``series_id`` on that stamp.
Once chosen, the label is kept until the next series.

Detector and amplifier names
----------------------------------------

The processor decodes detector names from ``sensor_index`` using
:ref:`sensor-identifiers`. Supply ``sensor_names`` to override particular
indices, for example ``{3: "R00_SG0"}``. An index from a different board
gets a diagnostic name such as ``sensor_00``; camera transformations
still require a detector that exists in the camera model.

Streaming offsets always use camera coordinates. The detector-to-amplifier
dictionary tells the combiner which amplifier contains each sensor's ROI.
The camera model then supplies that amplifier's flips and the detector's
rotation.

* With ``sensor_amplifiers=None`` (the default), the processor builds the
  dictionary from the first available ``segment`` for each sensor in the
  series. It clears this automatic mapping when the sequence changes.
* With a supplied dictionary, such as ``{"R00_SG0": "C05"}``, the processor
  uses that same dictionary across series. Stamp metadata neither adds
  missing sensors nor replaces their configured amplifiers. Supplying
  an empty dictionary also disables automatic mapping.

``set_sensor_amplifiers(mapping)`` replaces the current dictionary and
disables automatic mapping. Use it before processing a series.
Every sensor contributing an accepted offset needs an entry; a missing
entry raises ``KeyError``. Camera lookup and transformation errors also
propagate to the caller.

The streaming default differs from ``OffsetCombiner(None)``, which
leaves offsets in amplifier coordinates. The streaming processor can
build its camera mapping because it receives the DAQ metadata.
For automatic mapping, the source must have received the sensor's START
metadata before its stamps; the decoder's default segment value alone
does not identify the intended ROI amplifier.

When acquisitions are combined
----------------------------------------

Incoming stamps must be ordered by acquisition within a series, with
no repeated sensor/acquisition pairs. Sensor order within an acquisition
can vary. The processor uses these indices directly; it does not reorder
incoming data by timestamp or keep a duplicate-arrival history.

When a sensor that already has a reference receives a higher acquisition
index, the processor combines the previous live acquisition using the
measurements available then. It calls ``on_combined_offset`` with the
result. It does not wait for a fixed sensor count or a timeout.

Seed replay does not advance this live completion rule. All measurements
returned when a sensor first selects its reference, including the current
stamp's measurement, are treated as replay. They can fill acquisitions
that are still pending, but cannot change a result already combined.

For example, suppose sensors A and B each select a reference after two
stamps, with A arriving before B at every acquisition:

.. list-table:: Two sensors through reference selection and live processing
   :header-rows: 1
   :widths: 25 75

   * - Input
     - Result
   * - Acquisitions 0 and 1
     - Both sensors select references at index 1 and measure their seeds.
       Acquisitions 0 and 1 remain pending; no live offset is reported.
   * - Acquisition 2
     - Both sensors supply live measurements. Acquisition 2 remains pending.
   * - Acquisition 3
     - A's stamp closes acquisition 2, reporting one offset using A and B.
       B's stamp adds its measurement to acquisition 3.
   * - ``finalize()`` after stopping the source
     - Acquisitions 0, 1, and 3 are combined. The completion callback receives
       results 0, 1, 2, and 3 in order. No additional live callback is made.

If B is still selecting a reference when A closes an acquisition, only
A contributes. B may later measure a seed image for that acquisition,
but that late result is discarded. This allows live results to arrive
without waiting for every sensor to lock. An offline run can include
these later measurements and therefore need not produce identical
combined offsets; see :ref:`offset-combination`.

``n_total`` counts the measurements supplied for an acquisition, including
rejects; ``n_valid`` counts accepted offsets. Sensors that have not
selected a reference contribute no measurements. An acquisition with
only rejected measurements still produces a result with NaN offsets.
An index with no measurements produces no result. The combination guide
describes the scatter and standard-error fields, including the single-sensor
case.

Series changes and result callbacks
----------------------------------------

A different ``sequence`` finishes the previous series and starts another,
including a change from 65535 to 0. References, pending seeds, acquisition
bookkeeping, ROI records, and the automatic amplifier map are cleared.
The reset happens even when the ROI parameters repeat. A sequence change
can follow a slew or a restart; it does not by itself establish a new pointing.

Within one series, the processor extends the acquisition counter across
rollover. After raw index 65535, raw index 0 becomes 65536 in
``CombinedOffset.stamp_index``. The metadata retains the raw value.
With ordered input, a decrease in the raw counter is treated as rollover.

The callbacks announce series and deliver results:

* ``on_visit_start(sequence, label)`` announces the first stamp of a
  series after reading its metadata. The label can be empty; a label
  arriving later does not cause a second start callback.
* ``on_combined_offset(result)`` receives each acquisition completed live.
* ``on_visit_complete(sequence, offsets)`` receives all results for the
  ending series, sorted by acquisition index. It includes results already
  reported live and pending acquisitions combined at series completion.
  Combining those pending acquisitions does not call ``on_combined_offset``.
  A series where no sensor selected a reference completes with an empty list.

The previous series' completion callback runs before the new series'
start callback. ``combined_offsets`` retains results across all series,
sorting each series' portion when it ends. A position in that list is
not an acquisition index: indices can have gaps and repeat in different
series. Use the completion callback's sequence argument to associate
results with a series. Its list is new, but the result objects are shared
with ``combined_offsets``.

Threads, shutdown, and errors
----------------------------------------

``DaqStampSource`` uses one C++ worker thread to deliver all sensors'
stamps sequentially. That worker runs ``on_stamp`` and any callbacks it
invokes; no new thread is created for each callback. Direct callers must
also let one ``on_stamp`` call finish before starting the next.

On normal shutdown, stop the source and join its worker before calling
``finalize``. This combines the pending seed acquisitions and the final
live acquisition, then calls ``on_visit_complete``. Callbacks invoked by
``finalize`` run on the thread that called it. In either case, processing
waits for the callback to return, so callbacks should finish promptly.

Finalization does not attempt reference selection with a shorter seed
buffer or retry an unsuccessful selection. It releases images retained
by sensors that never selected a reference. References from successfully
locked sensors remain available until the next series.
Repeating a successful ``finalize`` does not repeat results or callbacks.
Later stamps from the finalized sequence are ignored; a different
sequence can begin another series on the same processor.

Unexpected tracker, camera, and callback exceptions propagate from the
processor. End the run and stop the source when they occur. Processing
may already have changed internal state, so a failed call or failed
finalization is not safely retryable. This differs from an unsuccessful
reference-selection attempt, which normally keeps collecting seeds.

The demo's ``run_live`` helper catches processing exceptions on the DAQ
worker, stops and joins the source, and raises the error on the calling
thread. This handling is needed because the Python binding otherwise
logs callback exceptions and contains them. Finalize only after a normal
return from ``run_live``; a processing or shutdown failure ends that run.

Metrics and memory use
----------------------------------------

``StreamingMetrics`` accumulates counts and timing samples across series:

* ``total_stamps`` counts ``on_stamp`` calls, including ignored calls after
  finalization and calls ending in error. ``seed_stamps`` counts inputs
  received before a sensor has a reference, including the successful
  reference-selection stamp.
* ``valid_measurements`` and ``invalid_measurements`` count returned
  measurements according to whether they are marked ``PASSED_QUALITY``.
  Both include seed replay. ``discarded_seed_measurements`` counts replay
  results omitted from already-combined acquisitions; these are a subset
  of the measurement counts, not additional measurements.
* ``lock_times`` records reference-selection attempts, including failed
  attempts and time spent evaluating possible reference stars.
  ``measure_times`` records each returned measurement, including rejects
  and seed replay.
* ``callback_times`` records the timed processing within ``on_stamp``,
  including tracking work, combination, and result callbacks.
  ``combine_times`` records only completed combiner calls and stops
  before notifying ``on_combined_offset``.

Durations use ``time.perf_counter`` and are in seconds. The timers cover
overlapping work, so their durations should not be added together.
They do not measure the delay from DAQ acquisition to result delivery.
``stream_seconds`` spans the first stamp callback's start to the latest
timed callback's end, including idle time between stamps. It uses the
local clock rather than DAQ timestamps.

The processor is intended for development runs of limited duration.
Combined results and timing lists grow throughout the run. A sensor
that cannot select a reference also retains a growing set of seed images
until the series ends. Starting a new series does not clear the lifetime
results or metrics; create a new processor for an independent run.

The demo's ``streaming_report`` module formats the metrics and results
for logging. Reporting is separate from the tracking and combination
operations described here.

API reference
----------------------------------------

``StreamingGuiderProcessor`` and ``StreamingMetrics`` are documented in
the pipeline API reference under :ref:`sensor-tracking`.
