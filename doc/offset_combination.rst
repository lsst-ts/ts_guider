.. py:currentmodule:: lsst.ts.guider.pipeline

.. _offset-combination:

Combining guider offsets
========================================

``MultiSensorRunner`` coordinates one ``SensorTracker`` per sensor and uses
``OffsetCombiner`` to produce a translation for each acquisition.
``run_offline`` drives the same calculation from one FITS sequence per sensor.
These tools support development and validation with recorded guider data
and provide a practical example of how the tracking and combination
components work together without a live DAQ connection.

From amplifier pixels to camera pixels
----------------------------------------

``OffsetCombiner`` uses a sensor's measurement only when it has passed the
quality checks (``state == MeasurementState.PASSED_QUALITY``) and that
sensor has a reference position.

The measurement and reference are separate ``CentroidMeasurement`` objects.
The reference stores the fixed position selected during detection. Its
state remains ``NOT_SET`` because it holds coordinates only; the quality
requirement above applies to the per-stamp measurement.

For each accepted measurement, ``measurement.offset_from(reference)``
subtracts the reference position from the measured position:

.. code-block:: text

   dx = measurement.x - reference.x
   dy = measurement.y - reference.y

These offsets initially follow the stamp's pixel axes: X increases along
columns and Y along rows. Position coordinates start at (0, 0) at the
center of the first pixel.

Different sensors can have different orientations. ``GuiderOrientation``
uses the LSST camera model to apply the amplifier's X/Y flips, followed by
the detector's rotation in multiples of 90 degrees. This expresses every
offset along the same camera axes, still in pixels, so the offsets can be
combined.

Only the offset from the reference position needs this transformation.
A constant shift of the coordinate origin would affect both positions
equally, so it cancels when we subtract them.

For example, for amplifier C05 in the camera model:

.. list-table:: Two sensors measuring the same camera offset
   :header-rows: 1
   :widths: 25 35 40

   * - Sensor
     - Local amplifier offset
     - Camera offset
   * - R00_SG0
     - ``(1, -2)``
     - ``(2, -1)``
   * - R04_SG0
     - ``(2, 1)``
     - ``(2, -1)``

The combined offset is the average of the accepted camera offsets for
one acquisition. Each contributing sensor has equal weight. The result
is an X/Y shift in camera pixels; it does not estimate rotation or average
across acquisitions.

For each axis:

* ``scatter`` measures how much the sensor offsets differ. It is their
  sample standard deviation (``ddof=1``).
* ``error`` estimates the uncertainty in the average:
  ``scatter / sqrt(n_valid)``.

This error estimate assumes independent sensor errors.

``CombinedOffset`` stores:

* ``stamp_index``: the acquisition index.
* ``combined_dx``, ``combined_dy``: the average X/Y offset.
* ``scatter_dx``, ``scatter_dy``, ``error_dx``, ``error_dy``: the scatter
  and estimated uncertainty for each axis.
* ``n_valid``: the number of offsets used in the average.
* ``n_total``: the number of supplied measurements, including rejects.
* ``per_sensor_offset``: the individual offsets used in the average,
  after transformation.
* ``measurements``: a copy of the input dictionary, including rejected
  measurements. Centroids remain in the original amplifier coordinates.

With one accepted sensor, its offset is returned, but scatter and error
are NaN. With none, the average, scatter, and error are all NaN.
The supplied measurements are kept in both cases.

The runner skips unknown sensors and sensors without a locked reference,
so neither is counted in ``n_total``.

Camera metadata requirements
----------------------------------------

Use ``sensor_amplifiers`` to choose the coordinate system when constructing
``OffsetCombiner`` or ``MultiSensorRunner``:

* **Camera coordinates:** pass a dictionary mapping sensor names to
  amplifiers, such as ``{"R00_SG0": "C05"}``. Every sensor contributing to
  the average needs an entry. The camera model supplies the amplifier
  flips and detector rotation.
* **Amplifier coordinates:** pass ``None`` to keep offsets in their
  original axes. Use this only when the offsets already share the same
  orientation.

An empty dictionary (``{}``) also requests camera coordinates, but contains
no amplifier entries. A contributing sensor without an entry raises
``KeyError``.

The named detectors and amplifiers are expected to exist in the camera
model. If a camera lookup or transformation fails, the original exception
is raised to the caller. The combiner does not substitute an untransformed
offset.

Coordinates used to calculate an offset must be finite. A NaN or infinite
value in the measurement or reference raises ``ValueError``. Measurements
that failed quality checks are kept for diagnostics and excluded from
the average.

``run_offline`` always uses camera coordinates and builds the amplifier
dictionary from FITS metadata. It gets the sensor name from ``RAFTBAY``
and ``CCDSLOT`` and requires ``ROISEG`` in every input sequence.
``ROISEG`` must be ``Segment`` followed by two digits from ``0`` to ``9``;
for example, ``Segment05`` selects amplifier ``C05``.

Offline FITS processing
----------------------------------------

``read_guider_sequence`` reads the emulator layout: a metadata-only primary
HDU followed by a two-dimensional image extension for each stamp.
Compressed image extensions are supported.
The reader returns a ``GuiderSequence`` containing a float32 stamp cube,
per-stamp MJD timestamps, the sensor name, and the segment string.

* Primary ``RAFTBAY=R00`` and ``CCDSLOT=SG0`` identify ``R00_SG0``.
  If both are absent, the reader uses the filename stem.
* Primary ``ROISEG=Segment05`` identifies amplifier ``C05``.
* Each image extension's ``STMPTMJD`` gives its timestamp; absent values
  become NaN.
* Extensions without two-dimensional images are skipped. A file without
  any image stamps raises ``ValueError``; stamps must have matching shapes
  to form a cube.

Supply one file per sensor with stamps already aligned by acquisition
index. Timestamps are preserved but are not used to align or validate
the sequences. Skipping an actual missing acquisition in one file would
shift this correspondence; the reader does not detect or repair that case.
Duplicate sensor names raise ``ValueError`` identifying both files;
multi-file concatenation is not implemented.

``run_offline`` feeds each sensor's images in acquisition order through
``SensorTracker.process_stamp``, using the lifecycle in :ref:`sensor-tracking`.
The first locking attempt uses ``config.seed_frames`` images. After an
unsuccessful attempt, the tracker keeps those images and retries with each
additional image, using the entire accumulated prefix. With the default
configuration this means attempts with 10, 11, 12, ... images until success
or the end of the common input range. All files have already been read into
memory, so expanding the prefix requires no wait for data to arrive.
Once a reference locks, later images do not change its position.

A successful lock replays all accumulated seeds, including rejected
measurements. The driver associates each returned measurement with its
original acquisition index. It combines the results after processing the
common range, allowing sensors that lock at different indices to contribute
to the same earlier acquisitions. This is an offline result history; it
does not reproduce the timing of live offset publication.

Processing stops at the shortest input sequence, including a sequence whose
sensor never locks. Images beyond this common range are not used for
reference selection or measurement. A sensor that has not locked by the
end contributes no measurements. If the common range contains fewer than
``seed_frames`` images, no early locking attempt is forced. Empty input,
or a run where no sensor locks, returns an empty list.

For direct use, ``MultiSensorRunner.lock_references`` takes explicitly
selected seed cubes and uses **all** supplied frames in one attempt,
independent of ``config.seed_frames``. It updates only the supplied known
sensors, resetting those trackers first; omitted trackers keep their state.
Its return value lists the sensors that locked on that call. Construct a
new runner for an independent run. ``process_acquisition`` expects aligned
stamps and an acquisition index supplied by the caller.

Every tracker shares the runner's mutable configuration. Direct field
changes affect subsequent processing without resetting references or
recomputing results; callers remain responsible for those changes.

Use the standard LSST environment with ``lsst.obs.lsst`` available for the
camera model, along with the package's tracking and CSC dependencies.
For example, after activating that environment and installing ts_guider:

.. code-block:: python

   from pathlib import Path

   from lsst.ts.guider.pipeline import GuiderTrackerConfig, run_offline

   results = run_offline(
       [Path("R00_SG0.fits"), Path("R04_SG0.fits")],
       GuiderTrackerConfig(seed_frames=10),
   )
   for result in results:
       print(
           result.stamp_index,
           (result.combined_dx, result.combined_dy),
           (result.error_dx, result.error_dy),
           result.n_valid,
       )

Reproducible validation
----------------------------------------

``tests/test_multi_sensor_runner.py`` generates noisy synthetic stars,
writes compressed FITS sequences with known camera offsets, and
runs the complete offline path against the real camera model.
It checks integer and fractional motion within 0.05 pixel for this
specific source/noise setup, expanding-prefix retries, seed replay,
sensors locking at different indices, finite short sequences,
shortest-sequence handling, and retained rejected measurements.
The companion tests cover amplifier flips and all four quarter turns,
real guider geometry, combination statistics, and zero/one-sensor cases.

.. code-block:: bash

   python -m pytest -q tests/test_guider_orientation.py \
       tests/test_offset_combiner.py tests/test_multi_sensor_runner.py

The implementation follows OSW-2858: ``sensor_orientation.py`` becomes
``guider_orientation.py``, ``batch.py`` becomes ``multi_sensor_runner.py``,
and ``GuiderSequence`` and ``CombinedOffset`` each have a matching module.
The combiner reuses the existing ``CentroidMeasurement.offset_from``
subtraction. DAQ identity helpers and streaming orchestration belong to
the subsequent live-processing stage.

API reference
----------------------------------------

The :ref:`sensor-tracking` API reference includes ``GuiderSequence``,
``CombinedOffset``, ``OffsetCombiner``, ``MultiSensorRunner``,
``read_guider_sequence``, ``build_sensor_amplifiers``, and ``run_offline``.
The camera helpers are documented below.

.. automodapi:: lsst.ts.guider.guider_orientation
   :no-main-docstr:
   :no-inheritance-diagram:
