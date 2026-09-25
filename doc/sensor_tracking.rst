.. py:currentmodule:: lsst.ts.guider.pipeline

.. _sensor-tracking:

Single-sensor guide-star tracking
========================================

``SensorTracker`` selects a reference guide star and measures its position
in subsequent image stamps.
The reference position and measured centroids use zero-based stamp pixels:
the center of the first pixel has coordinates (0, 0), x increases along columns,
and y increases along rows.
``measurement.offset_from(tracker.reference)`` subtracts the reference
position from the measurement to give a displacement in the incoming
amplifier image's pixel axes.

For example, a reference at ``(100, 80)`` and a measurement at ``(102, 79)``
give ``dx = +2`` and ``dy = -1`` pixels.
The next stage expresses these displacements in common camera axes and
combines sensors; see :ref:`offset-combination`.

Processing individual stamps
----------------------------------------

Call ``tracker.process_stamp(stamp)`` once for each incoming image.
The tracker owns the seed-image buffer, decides when to lock, and measures
subsequent stamps against the fixed reference. Acquisition indices and
series metadata belong to the caller, not this class.

``get_status()`` returns a ``GuidingStatus``:

* ``NONE``: newly constructed or reset, with no pending images.
* ``LOCKING``: collecting seeds and selecting a guide star. Detection finds
  candidates in the stacked image. GalSim adaptive-moment (HSM) fits on
  individual seeds check whether those candidates can be measured reliably.
* ``LOCKED``: a star has been selected and its reference position is fixed.
  HSM fits measure that star in individual stamps to calculate offsets.
  A rejected measurement does not change this state or the reference.
* ``LOST``: reserved for a future loss and recovery policy. Automatic
  transitions to this state are not implemented, and no rejection-count
  threshold is assumed.
* ``ERROR``: malformed input or an unexpected processing exception. The
  exception is retained in ``last_error`` and re-raised to the caller.
  Processing requires an explicit ``reset()`` or ``lock_reference`` call.

After locking, ``last_measurement.state`` tells the caller whether
the latest measurement is usable: it must be ``MeasurementState.PASSED_QUALITY``.
A rejected result is still stored in
``last_measurement``, including any available centroid coordinates,
signal-to-noise ratio, source width and shape, and the measurement state
indicating fit success and quality. These values help explain why the
measurement was rejected.

``get_offsets()`` returns the current ``(dx, dy)`` in amplifier pixels when
the tracker is ``LOCKED``, both the reference and latest measurement have
finite ``x`` and ``y`` coordinates, and the latest measurement passes the
quality checks. Otherwise, it raises ``RuntimeError``. It never substitutes
an earlier good measurement for the latest rejected result.

``get_measurements()`` exposes only the latest call's results:

* An empty tuple while ``LOCKING``. HSM fits made during this stage are
  used only to evaluate candidates.
* Every seed measurement, in input order, when locking succeeds and
  ``process_stamp`` replays the seeds through ``measure``.
  Rejected measurements keep their positions in the tuple so a caller can
  pair results with acquisition indices without shifting their association.
* One measurement per subsequent stamp, including rejected measurements.

You can call ``get_measurements()`` repeatedly to read the same results.
Processing the next stamp replaces those results, so callers must save
any measurements they want to keep.

While collecting seeds, the tracker stores its own copy of each image.
The caller can therefore reuse or modify its input array after
``process_stamp()`` returns. ``pending_seed_count`` reports how many seed
images are currently stored.

``lock_duration`` records the time spent selecting a reference during the
latest ``process_stamp()`` call, including the candidate HSM fits, in
seconds. It is ``None`` if no locking attempt was made.
``measurement_durations`` contains the time for each measurement after
locking, also in seconds and in the same order as ``get_measurements()``.
The next ``process_stamp()`` call replaces both sets of timings.

Selecting the reference (LOCKING)
----------------------------------------

During ``LOCKING``, the tracker accumulates seeds and may find several
candidate guide stars. None is the fixed reference until one is selected.
The first attempt starts after ``GuiderTrackerConfig.seed_frames`` stamps
have arrived (10 by default). Each attempt uses all accumulated seeds:

1. **Detection in the stack** finds connected groups of bright pixels
   (footprints) in the median image and calculates a brightness-weighted
   position for each candidate.
2. **HSM on individual seeds** checks which candidates can be measured
   reliably in at least ``min_valid_stamp_fraction`` of the seeds.
3. Select the qualifying candidate with the highest median accepted SNR.
   Its **position detected in the stack** becomes the fixed ``reference``,
   and the state changes to ``LOCKED``.

If no candidate qualifies, the tracker keeps the seeds and retries with
each new stamp: ten, eleven, twelve, and so on with the default settings.
There is no automatic image-count or time limit. ``reset()`` discards the
seeds when the caller wants to abandon acquisition or begin a new series.

Measuring stamps (LOCKED)
----------------------------------------

After selecting the reference, ``process_stamp`` calls ``measure`` on every
accumulated seed again, in arrival order. This produces the measurements
returned by ``get_measurements()``. The seed images are then released.
Each subsequent ``process_stamp`` call measures just the incoming stamp.

For each stamp, ``measure`` extracts a fixed-size cutout around the locked
reference, subtracts an annulus background, and fits the star with HSM.
It converts the fitted position to zero-based full-stamp coordinates.
The offset is this HSM position minus the fixed position detected in the
stack.

We have created unit tests using synthetic images to check that a
stationary star gives approximately zero offset and that a deliberately
shifted star gives the expected displacement.

While ``LOCKED``, the tracker uses the same HSM fitting and quality checks
used to evaluate candidates during ``LOCKING``.
Each measurement is represented by a ``CentroidMeasurement`` object with
these fields:

.. list-table:: CentroidMeasurement fields
   :header-rows: 1
   :widths: 25 75

   * - Field
     - Meaning
   * - ``x``, ``y``
     - Measured centroid in full-stamp amplifier pixels.
   * - ``snr``
     - Aperture signal-to-noise ratio, including configured detector gain.
   * - ``fwhm``
     - Gaussian-equivalent width, ``2.355 * moments_sigma``, in pixels.
   * - ``e1``, ``e2``
     - Adaptive-moment distortion components describing source shape.
   * - ``state``
     - A ``MeasurementState`` combining fit convergence and quality.

``MeasurementState`` describes one measurement independently of the
tracker's ``GuidingStatus``:

* ``NOT_SET``: no measurement outcome is available. This is the default,
  also used for the reference that contains only detected coordinates.
* ``NOT_CONVERGED``: the cutout was unusable or the HSM fit did not converge.
* ``CONVERGED``: the fit succeeded, but the result failed a quality cut.
* ``PASSED_QUALITY``: the fit succeeded and passed all quality cuts.

Quality checks require a successful fit, finite coordinates and shape/SNR
values, sufficient SNR, acceptable ellipticity and width, and a centroid
inside the allowed stamp area.
With margin ``m``, the permitted coordinates satisfy
``m <= x < n_columns - m`` and ``m <= y < n_rows - m``.
Rows and columns are checked independently for rectangular stamps.

A failed fit or unusable cutout returns a ``NOT_CONVERGED`` measurement
with non-finite coordinates. A successful fit that fails a quality cut
retains its diagnostic values with state ``CONVERGED``.
``offset_from`` requires another ``CentroidMeasurement`` and raises
``ValueError`` if either object's coordinates are non-finite, or ``TypeError``
for an unsupported reference type. The method checks that the coordinates
are finite but does not do any quality checks.

Callers must require ``measurement.state == MeasurementState.PASSED_QUALITY``
before accepting a guiding offset;
``SensorTracker.get_offsets()`` includes that check.

Calling ``measure`` without a locked reference raises ``RuntimeError``;
malformed seed cubes or a changed stamp shape raise ``ValueError``.

Pixels containing NaN or infinity cause a measurement to be rejected if they
fall inside the star’s measurement cutout. Such pixels elsewhere in the
stamp do not automatically reject the measurement.

Supplying a seed cube directly
----------------------------------------

``lock_reference(seed_stamps)`` is an alternative for callers that manage
their own seed images. You must supply the complete cube to use for that
attempt. The method clears any internally buffered seeds, previous
reference, measurements and error state.
It uses every supplied frame, without waiting for ``seed_frames`` images.
It performs the same candidate detection and HSM checks as automatic
processing.

On success, it returns ``True`` and sets ``LOCKED``, but does not replay
the seeds. Call ``measure(stamp)`` to obtain a measurement before requesting
an offset. If no candidate qualifies, it returns ``False`` and leaves the
tracker in ``LOCKING`` with no reference or buffered seeds.
``process_stamp`` handles seed accumulation, retries, and replay
automatically instead.

Resetting and changing configuration
----------------------------------------

``reset()`` clears the reference, pending seeds, measurements, and error
state while keeping the sensor name and current configuration. It discards
any accumulated seed images without attempting to lock them.

``GuiderTrackerConfig`` is a mutable dataclass shared by the tracker and
its detector. Update its fields directly to change settings for subsequent
processing. Changes do not reset the tracker or recompute its existing
reference and measurements. Call ``reset()`` when the updated settings
should apply to a new reference window:

.. code-block:: python

   tracker.config.min_snr = 20.0
   tracker.reset()

To use a separate configuration object, pass it to ``reset(config=...)``.
This starts a new seed window and gives the tracker and detector the same
replacement configuration. ``dataclasses.replace`` can create a copy with
selected fields changed:

.. code-block:: python

   from dataclasses import replace

   tracker.reset(config=replace(tracker.config, min_snr=20.0))

Example
----------------------------------------

The algorithm requires NumPy, SciPy, Astropy, and GalSim, declared in the
package's Python dependencies.
Use the standard LSST environment with salobj and ts_xml available, because
importing ``lsst.ts.guider`` also imports the CSC.
Tracking does not require a camera model or a live DAQ connection.
Run this example with the package installed, or put the checkout's ``python``
directory on ``PYTHONPATH``:

.. code-block:: python

   import numpy as np

   from lsst.ts.guider.pipeline import (
       GuiderTrackerConfig, GuidingStatus, MeasurementState, SensorTracker,
   )

   rng = np.random.default_rng(42)
   rows, columns = np.indices((100, 160))

   def make_stamp(x, y):
       sigma = 2.0
       star = 100_000 / (2 * np.pi * sigma**2) * np.exp(
           -((columns - x)**2 + (rows - y)**2) / (2 * sigma**2)
       )
       return (100 + star + rng.normal(0, 5, rows.shape)).astype(np.float32)

   tracker = SensorTracker("example", GuiderTrackerConfig())
   for _ in range(tracker.config.seed_frames):
       tracker.process_stamp(make_stamp(105.25, 45.7))
   assert tracker.get_status() == GuidingStatus.LOCKED
   assert len(tracker.get_measurements()) == tracker.config.seed_frames

   tracker.process_stamp(make_stamp(107.25, 44.7))
   assert tracker.get_status() == GuidingStatus.LOCKED
   assert tracker.last_measurement.state == MeasurementState.PASSED_QUALITY
   offset = tracker.get_offsets()
   np.testing.assert_allclose(offset, (2.0, -1.0), atol=0.05)
   print(f"Reference: {tracker.reference_center}; offset: {offset}")

   # A rejected stamp does not discard the established reference.
   tracker.process_stamp(np.zeros(rows.shape))
   assert tracker.get_status() == GuidingStatus.LOCKED
   assert tracker.last_measurement.state == MeasurementState.NOT_CONVERGED
   # get_offsets() would raise for this stamp; there is no stale fallback.

The 0.05-pixel tolerance applies to this bright synthetic Gaussian and
noise level; it is not a general accuracy guarantee for observed stars.
The tests also cover fractional-pixel displacements, rectangular stamps,
bright column artifacts, failed locking, and quality rejection.

API reference
----------------------------------------

.. automodapi:: lsst.ts.guider.pipeline
   :no-main-docstr:
   :no-inheritance-diagram:
