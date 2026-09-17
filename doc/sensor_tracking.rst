.. py:currentmodule:: lsst.ts.guider.pipeline

.. _sensor-tracking:

Single-sensor guide-star tracking
========================================

``SensorTracker`` selects a reference guide star and measures its position
in subsequent image stamps.
The reference position and measured centroids use zero-based stamp pixels:
x increases along columns, and y increases along rows.
``measurement.offset_from(tracker.reference_center)`` subtracts the reference
position from the measurement to give a displacement in the incoming
amplifier image's pixel axes.

For example, a reference at ``(100, 80)`` and a measurement at ``(102, 79)``
give ``dx = +2`` and ``dy = -1`` pixels.
The next stage expresses these displacements in common camera axes and
combines sensors; see :ref:`offset-combination`.

Selecting the reference
----------------------------------------

``lock_reference`` uses up to ``GuiderTrackerConfig.seed_frames`` initial
stamps; the default is 10.
A shorter nonempty sequence is accepted, and the caller chooses when enough
frames have arrived to attempt reference selection.

The selection proceeds as follows:

1. Form a median reference image from the seed frames.
   A deterministic uniform dither breaks integer quantization before the
   median; a single seed frame is used directly.
2. Detect connected footprints above a background/noise threshold and apply
   minimum-footprint-size and edge cuts.
3. Measure each candidate with GalSim adaptive moments on every seed frame.
   Each candidate must pass the quality cuts on at least the configured
   ``min_valid_stamp_fraction`` of the available seed frames.
4. Select the accepted candidate with the highest median accepted
   signal-to-noise ratio.
   Store its detected reference-image position as ``reference_center``.

The reference position is the detected center in the seed reference image;
the later centroids come from adaptive-moment fits on individual stamps.
These are different estimators, so the tests check the stationary-source
offset as well as injected motion.
The reference remains fixed until the next call to ``lock_reference``.
An unsuccessful locking attempt returns ``False`` and leaves no reference,
including when a previous attempt succeeded.

Measurements and quality
----------------------------------------

For each stamp, ``measure`` extracts a fixed-size cutout around the locked
reference, subtracts an annulus background, and fits adaptive moments.
It converts GalSim's coordinates back to zero-based full-stamp coordinates.
The measurement contains:

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
   * - ``converged``
     - Whether the adaptive-moment fit reported success.
   * - ``passed_quality``
     - Whether the result passed all configured acceptance cuts.

Quality checks require a successful fit, finite coordinates and shape/SNR
values, sufficient SNR, acceptable ellipticity and width, and a centroid
inside the allowed stamp area.
With margin ``m``, the permitted coordinates satisfy
``m <= x < n_columns - m`` and ``m <= y < n_rows - m``.
Rows and columns are checked independently for rectangular stamps.

A failed fit or non-finite cutout returns an invalid measurement.
A successful fit that fails a quality cut retains its diagnostic values.
``offset_from`` performs subtraction without filtering: callers must check
``passed_quality`` before accepting the offset.
Calling ``measure`` without a locked reference raises ``RuntimeError``;
malformed seed cubes or a changed stamp shape raise ``ValueError``.
For a locked reference, non-finite pixels outside the measurement cutout
do not invalidate an otherwise measurable source.

Reproducible example
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

   from lsst.ts.guider.pipeline import GuiderTrackerConfig, SensorTracker

   rng = np.random.default_rng(42)
   rows, columns = np.indices((100, 160))

   def make_stamp(x, y):
       sigma = 2.0
       star = 100_000 / (2 * np.pi * sigma**2) * np.exp(
           -((columns - x)**2 + (rows - y)**2) / (2 * sigma**2)
       )
       return (100 + star + rng.normal(0, 5, rows.shape)).astype(np.float32)

   tracker = SensorTracker("example", GuiderTrackerConfig())
   seeds = np.stack([make_stamp(105.25, 45.7) for _ in range(10)])
   assert tracker.lock_reference(seeds)

   measurement = tracker.measure(make_stamp(107.25, 44.7))
   assert measurement.passed_quality
   offset = measurement.offset_from(tracker.reference_center)
   np.testing.assert_allclose(offset, (2.0, -1.0), atol=0.05)
   print(f"Reference: {tracker.reference_center}; offset: {offset}")

The 0.05-pixel tolerance applies to this bright synthetic Gaussian and
noise level; it is not a general accuracy guarantee for observed stars.
The tests also cover fractional-pixel displacements, rectangular stamps,
bright column artifacts, failed locking, and quality rejection.

API reference
----------------------------------------

.. automodapi:: lsst.ts.guider.pipeline
   :no-main-docstr:
   :no-inheritance-diagram:
