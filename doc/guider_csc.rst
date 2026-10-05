.. _guider-csc:

Guider CSC processing and publication
========================================

``GuiderCsc`` runs the :ref:`streaming-guider-pipeline` while ENABLED and
publishes its results through salobj. The camera's DAQ stream determines
when images arrive and when a new guide series begins. Use the standard
CSC enable/disable commands to control the subscription.

Use :ref:`guider-installation` to prepare the Python package, camera model,
and DAQ binding. The :ref:`sensor-tracking` and :ref:`offset-combination`
guides describe the measurements and camera transformations consumed by
the CSC.

Interface and configuration
----------------------------------------

The CSC and its Remotes use the Guider interface provided by ``ts_xml``.

``Guider v1`` configuration accepts:

.. list-table:: Configuration
   :header-rows: 1
   :widths: 22 15 63

   * - Property
     - Default
     - Meaning
   * - ``partition``
     - Required
     - DAQ/GDS partition used in regular operation. The configuration
       schema also requires this string in simulation mode.
   * - ``seed_frames``
     - 10
     - Minimum stamps per sensor before its first reference-selection
       attempt; an integer of at least one.
   * - ``min_snr``
     - 10.0
     - Minimum aperture signal-to-noise ratio for an accepted measurement.

For each run, the CSC creates a ``GuiderTrackerConfig`` and applies the
``seed_frames`` and ``min_snr`` values from its YAML configuration. When
either value is omitted, it keeps the corresponding tracker default shown
above. Other tracker properties retain their defaults defined in Python;
they are not configurable through the CSC's YAML configuration. Code that
uses the tracker directly can change those properties programmatically.

Meeting ``min_snr`` alone does not make a measurement acceptable. The
tracker also requires:

* **A successful fit with finite values.** The centroid position, SNR,
  width, and shape must contain neither NaN nor infinity.
* **Acceptable ellipticity.** ``max_ellipticity=0.7`` limits the shape's
  distortion magnitude, ``hypot(e1, e2)``. A larger value indicates a source
  that is too elongated to accept.
* **Acceptable width.** The full width at half maximum (FWHM) must be
  positive and no greater than ``max_fwhm=15.0`` pixels. This rejects
  invalid sizes and overly broad sources, even when their SNR is high.
* **A centroid within the allowed stamp area.** ``edge_margin=5`` excludes
  a five-pixel margin around the stamp's edges. A centroid in that margin
  is rejected.

The ellipticity, width, and edge-margin values above are the defaults used
by the CSC. A bright source can therefore pass the SNR requirement and
still be rejected for its shape, size, or position. See
:ref:`sensor-tracking` for the measurement and acceptance rules.

The tracker maintains a separate stack of stamp images for each sensor.
If reference selection fails, it retains that sensor’s images, adds each
newly received stamp to the stack, and retries reference selection using
the full stack.  Once a reference is selected, it
measures the buffered images and subsequent arrivals. ``seed_frames`` is
therefore a starting threshold, not a fixed window or a guarantee of lock.
A new DAQ sequence starts reference selection again.

Regular operation (simulation mode 0) subscribes a
``guiderGDS.DaqStampSource`` to the configured partition. Simulation mode 1
creates the same processor and publisher without importing or subscribing
to ``guiderGDS``. A Python test or driver must supply images and metadata
by calling ``process_stamp`` on the CSC instance, one stamp at a time in
acquisition order. Simulation does not generate images itself.

A more detailed explanation and a worked example of supplying simulation
images may be added in future documentation.

The entry point is ``run_guider_csc``. SAL index 1 selects MainTel and
index 2 selects AuxTel, but the current camera transformations use LSSTCam
for both. Selecting index 2 does not provide an AuxTel-specific camera
model.

Processing and thread handoff
----------------------------------------

Enabling prepares the CSC to turn incoming stamp images into star-offset
measurements and publish the results. In regular operation, the C++ DAQ
source receives images on a worker thread, which calls the Python image
processing code. The CSC's asyncio event loop runs the asynchronous work,
including command handling and topic writes.
A queue holds samples waiting to be published, allowing image processing
to finish without waiting for those topic writes to complete. Each enable
creates a fresh processor, queue, and publishing task.

Before subscribing to images, the CSC loads the camera model. This model
provides the sensor rotations and amplifier flips needed to express all
offsets in the same camera coordinates. It is normally built on first
use. Leaving that work until the first offset transformation would hold
up the DAQ worker inside image processing: the worker cannot return to
receiving stamps until that call finishes. Loading the model before
subscription puts this cost in startup instead. The load runs in an
executor, a separate thread used for blocking work, so the asyncio event
loop does not have to perform it directly.

Each incoming stamp follows this path:

1. The C++ binding calls the Python method ``process_stamp`` on the DAQ
   worker thread. This is a synchronous call: the worker waits for the
   method to return before continuing. The method records the sensor,
   acquisition identity, and ROI metadata, then passes the image to
   ``StreamingGuiderProcessor.on_stamp``. ``SensorTracker`` selects the
   reference and measures the star; ``OffsetCombiner`` transforms and
   combines sensor offsets.
2. When the processor has a live combined result, or a series starts or
   finishes, it calls the corresponding CSC callback. These callbacks
   are functions registered with the processor to handle those events.
   They prepare the field values for each topic sample: offsets,
   quality flags, sensor lists, and acquisition identity where applicable.
   The queued values describe that particular result; processing the next
   image does not replace them with newer values. The CSC uses the
   processor's measurements without measuring the image a second time.
3. The callback uses ``call_soon_threadsafe`` to ask the asyncio event loop
   to insert the prepared sample into the queue. This transfers work
   safely from the DAQ thread to the event loop, where the queue is used.
   The publishing task, ``_publish_loop``, takes samples from the queue
   and calls ``await topic.set_write(...)`` to fill and write each sample
   through salobj. It waits for one write to finish before starting the
   next.

This publishing task writes the five science topics listed below. The
DAQ worker prepares their values but does not wait for their network
publication. Standard CSC events, such as ``summaryState``, ``errorCode``
and ``logMessage``, use salobj's own publication paths.

Each queue belongs to one enabled run. If an old callback's request to
add a sample arrives after its queue has closed, the CSC discards it.
That prevents results from a previous run appearing in a newly enabled
run's queue.

The source must deliver each sensor/stamp pair once, in acquisition order,
with one processing call at a time. Simulated callers must do the same.
The CSC does not sort out-of-order arrivals or wait for a predefined set
of sensors before progressing. The publication queue has no size limit,
and its growth does not pause stamp processing. If writing topics stays
slower than processing images, pending samples accumulate in memory.
Processor histories and reference buffers also use memory, as described
in :ref:`streaming-guider-pipeline`.

Shutdown and recovery
----------------------------------------

Disabling, entering FAULT, or closing the CSC must finish the current run
before its processing state can be released. They use the same cleanup
operation. If two requests arrive together, both wait for that operation;
they do not independently stop the source or finalize the processor.
Startup also waits for cleanup. Cancelling a request that is waiting for
cleanup does not interrupt the cleanup itself.

A healthy stop proceeds in this order:

1. Stop accepting new stamps for processing and ask the DAQ source to
   stop. Its stop method waits for the worker thread to exit; this wait
   is called a *join*. The CSC runs the stop method in an executor so
   the asyncio event loop can continue handling other work while it waits.
2. Once the worker has exited, finish the processor's pending work if
   the run has no recorded failure. Waiting for the worker first ensures
   an image cannot still be modifying the processor during finalization.
   Finalization completes the series and can queue its final STOP event;
   it does not publish pending offsets as new guiding corrections.
3. Close the queue (draining the queue) to new samples and allow the
   publishing task to attempt all samples already accepted, including the
   final STOP event if one was generated. End the publishing task after
   it reaches the end of the queue.

The source-stop wait has a **ten-second deadline**. If the stop method
raises an exception or the deadline expires, the CSC cannot confirm that
the worker has exited. It therefore keeps the source and processor
objects, skips finalization, and rejects further input and new publication
requests. A stop call that timed out may still be running in the executor;
the CSC retains a reference to that operation and logs its eventual
outcome. The current recovery policy requires a CSC process restart after
either a failed or timed-out source stop, even if it later succeeds. The
CSC does not retry that stop or allow another enable to reuse the source.

Publication drain has a **separate ten-second deadline**. If a topic write
stalls, the CSC logs the failure and number of queued samples, discards
those samples, and cancels the publishing task. If that task does not
respond to cancellation, the CSC keeps track of it and refuses to start
another run until it exits. A completed drain means the queued writes
were attempted; it does not confirm that a Remote received every sample.

These deadlines govern two successive waits; they are not a single
ten-second limit for shutdown. In particular, the source deadline does
not terminate the native thread. Its join and executor shutdown can still
prevent a clean process exit. The deadlines are fixed implementation
constants, not configuration properties.

Startup or processing failures also prevent finalization. An exception
during finalization is retained, and finalization is not retried. The
first run failure determines the reported error; secondary cleanup errors
are logged and attached to it. Previously accepted payloads may still be
drained, but a failed run does not guarantee a final STOP event.

.. list-table:: Failure codes
   :header-rows: 1
   :widths: 12 30 58

   * - Code
     - Name
     - Meaning
   * - 1
     - ``DAQ_SUBSCRIPTION``
     - Startup failed while constructing the processor, loading the
       camera, importing the binding, or constructing/starting the source.
   * - 2
     - ``STAMP_PROCESSING``
     - Stamp processing or processor finalization failed.
   * - 3
     - ``SHUTDOWN``
     - Source shutdown or publication drain failed or exceeded its
       deadline, with no earlier run failure taking precedence.

Startup, processing, and cleanup failures during state transitions fault
the CSC. If image processing raises an exception on the DAQ worker, the
CSC asks the event loop to report the fault and start cleanup. The image
callback can then return, allowing the worker to exit when asked to stop.
Trying to join the worker from within its own callback would make it wait
for itself. Closing the CSC still attempts salobj cleanup even when
stopping the run fails.

After an ordinary processing or subscription failure with successful
cleanup, the CSC can recover through FAULT, STANDBY, DISABLED, and ENABLED
using the standard transitions. Re-enable creates a fresh processor and
queue. An already reported error does not block returning to STANDBY, but
the source and publisher checks above still apply before starting a run.

If an individual topic write raises an exception, the publishing task
logs the exception, skips that sample, and attempts the next queued one.
It does not retry the failed write, and that exception alone does not
fault the CSC. For example, a failed offsets write does not prevent the
following summary-results write from being attempted. A Remote may
therefore see an incomplete set of topics for an acquisition while the
CSC remains ENABLED. Monitor ``logMessage`` for these write failures.

Published results
----------------------------------------

The CSC publishes measurements and combined offsets through the five
topics below. The ``stateMetadata``, ``summaryResults``, and
``perGuiderResults`` events include fields that tell readers which images
the results describe:

* ``seqno``: the DAQ sequence number, identifying the guide series.
* ``stamp``: the stamp number supplied by the DAQ, shared by the sensor
  images from the same acquisition.
* ``timestamp``: when those images were acquired, in TAI seconds.
* ``obsid``: the observation name, or the series label if no observation
  name was supplied.

A new DAQ sequence ends the preceding series and starts reference
selection again. A healthy series also ends when the CSC stops guiding;
a pause in incoming images alone does not end it. The STOP event describes
the series that just ended. Changing only ``obsid`` does not start a new
series or reset the reference.

.. list-table:: Science topics
   :header-rows: 1
   :widths: 24 76

   * - Topic
     - What it reports
   * - ``stateMetadata`` event
     - START at the first stamp, STAMP for each live combined acquisition,
       and STOP on successful series completion. Includes the fields
       above and the applicable observed-sensor list.
   * - ``seriesMetadata`` event
     - ROI metadata at the first live combine of each series. Identical
       ROIs are explicitly republished for a new series. A series without
       a live combine has no seriesMetadata event.
   * - ``offsets`` telemetry
     - Combined camera-frame ``x`` and ``y`` translations in millimeters.
       ``rotation`` is unavailable and is NaN.
   * - ``summaryResults`` event
     - ``deltaX`` and ``deltaY`` in camera-frame millimeters,
       ``goodStamps``, ``qualityFlag``, and the fields above.
       ``deltaRotation`` is unavailable and is NaN.
   * - ``perGuiderResults`` event
     - Named-sensor ``centroidDx``/``centroidDy`` offsets in camera-frame
       millimeters, fit quality, moments, and the fields above.

During guiding, the processor combines measurements for one stamp number
when a sensor that already has a reference provides a later stamp. For
example, the first such measurement for stamp 11 triggers the combined
result for stamp 10. Measurements from images saved during reference
selection can contribute if that result has not yet been combined.
Results already published are not revised.

When a series ends normally, the processor finishes the remaining
calculations, including those for the last stamp. It keeps those results
in its internal record without sending them as new guiding corrections.
The CSC publishes a STOP event to mark the end of the series. See
:ref:`streaming-guider-pipeline` for more detail.

For each result published during guiding, the CSC queues ``stateMetadata``
(STAMP), ``offsets``, ``summaryResults``, and ``perGuiderResults`` in that
order. For the first result in each series, ``seriesMetadata`` comes first.
Each topic is written separately.

To match event samples for the same result, compare their ``seqno``,
``stamp``, and ``timestamp`` values. The ``offsets`` telemetry has no
``seqno`` or ``stamp`` fields.

Reading the measurements
----------------------------------------

Matching array entries to sensors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Sensor names are sorted and separated by colons. For example,
``R00_SG0:R00_SG1`` means array entry 0 belongs to ``R00_SG0`` and entry 1
to ``R00_SG1``.

The topics can list different sensors:

* ``stateMetadata.sensors`` names sensors that supplied images: those
  seen so far at START, those for the current stamp at STAMP, and all
  those from the series at STOP.
* ``seriesMetadata.sensor`` lists sensors whose ROI information was
  available when the first guiding result was published. This list stays
  fixed for the series.
* ``perGuiderResults.sensor`` lists sensors measured for this result,
  whether accepted or rejected. Sensors with no measurement are omitted.

Use ``perGuiderResults.sensor`` for the result arrays, including
``summaryResults.qualityFlag``. Use ``seriesMetadata.sensor`` for the ROI
arrays.

Every per-sensor array has eight entries. In the example above, the last
six are unused. Unused entries contain NaN in result arrays, zero in ROI
integer arrays, and false in ``splitroi``. Data from a ninth distinct
sensor in a series causes a processing error.

Checking measurement quality
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``summaryResults.qualityFlag`` and ``perGuiderResults.centroidFitQuality``
use the same values:

* **1:** accepted; passed the tracker's acceptance checks.
* **0:** rejected; failed fit or failed acceptance check.
* **NaN:** unused array position.

Rejected measurements have NaN offsets and do not contribute to the
combined offset. They can still contain star shape information if the
fit succeeded.

``goodStamps`` counts the sensors contributing to this combined offset.
It does not count images collected over time. With no contributors, the
combined offset is NaN. With one, an offset is available but the scatter
and standard error are not.

Offsets and star shapes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An offset is the measured star position minus its locked reference
position. The tracker establishes the reference using the sensor's saved
stack of images. Sensor rotations and amplifier flips put the offsets
into common camera coordinates. Offsets are published in millimeters,
using ``0.010`` mm per pixel.

``momentXx``, ``momentYy``, and ``momentXy`` describe the size and shape of
the fitted star. They are published in mm squared and use the stamp's
own image axes (the amplifier/ROI frame). They do not use the camera axes
of the offsets. Failed fits or invalid shapes give NaN moments.

.. admonition:: How the shape moments are calculated

   The CSC reconstructs the moments from the tracker's FWHM and shape
   components ``e1`` and ``e2``. GalSim HSM defines its size as
   ``sigma = det(M)**(1/4)``, where ``M`` is the moment matrix; see the
   `GalSim HSM definition <https://galsim-developers.github.io/GalSim/_build/html/hsm.html>`_.
   The tracker stores ``fwhm = FWHM_PER_SIGMA * sigma`` in pixels. The
   shared constant ``FWHM_PER_SIGMA = 2.355`` is the rounded Gaussian
   conversion factor ``2 * sqrt(2 * ln(2))``. The conversion to mm squared
   is::

       sigma_mm = fwhm / FWHM_PER_SIGMA * 0.010
       half_trace = sigma_mm**2 / sqrt(1 - e1**2 - e2**2)
       momentXx = half_trace * (1 + e1)
       momentYy = half_trace * (1 - e1)
       momentXy = half_trace * e2

   Moments require a successful fit: ``CONVERGED`` or ``PASSED_QUALITY``.
   FWHM must be finite and positive, and ``hypot(e1, e2)`` must be below
   one. The computed half-trace must also be finite; otherwise the moments
   are NaN.

Fields that are not yet measured
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

These fields do not yet contain measurements:

.. list-table:: Unavailable fields
   :header-rows: 1
   :widths: 38 12 50

   * - Fields
     - Value
     - Meaning
   * - ``rotation``, ``deltaRotation``
     - NaN
     - Rotation offsets are not measured.
   * - ``flux``, ``fluxErr``
     - NaN
     - Flux and its error are not reported. Their interface units are
       electrons.
   * - ``centroidX``, ``centroidY``, ``centroidXErr``, ``centroidYErr``
     - NaN
     - Absolute focal-plane positions and their errors are unavailable.
       Measured offsets are in ``centroidDx`` and ``centroidDy``.
   * - ``roiCommonIntegration``
     - 0
     - Integration time is unavailable from the stamp binding. Zero is
       a placeholder.
   * - ``splitroi``
     - False
     - Split-ROI information is unavailable from the stamp binding.
       False does not confirm an unsplit ROI.

Diagnostics and topic watcher
----------------------------------------

Use ``watch_guider_topics`` to monitor the CSC's measurements, status, and
diagnostic messages. The measurement topics provide the guiding results.
Log messages provide additional information, including errors and offset
uncertainty and scatter.

Use the same Kafka and topic settings as the CSC:
``LSST_KAFKA_BROKER_ADDR``, ``LSST_SCHEMA_REGISTRY_URL``, ``LSST_SITE``, and
``LSST_TOPIC_SUBNAME``. Then run::

    watch_guider_topics --index 1

Match ``--index`` to the CSC: 1 for MainTel (the default), or 2 for AuxTel.
The watcher displays topic offsets in micrometers (``um``); log offsets
remain in millimeters. Press Ctrl-C to stop. Use
``watch_guider_topics --help`` to see the options.

Validation boundaries
----------------------------------------

Simulation can check configuration, reference selection, processing,
publication, and Remote receipt without a DAQ subscription. Feeding stamps
from a Python worker thread also exercises the callback-to-event-loop
handoff. These checks do not exercise the native SDK, DAQ discovery,
network delivery, or C++ worker shutdown. Deployment validation requires
the real binding/source/CSC/Kafka path as a separate check; a successful
queue insertion or simulated topic receipt does not establish that path.
