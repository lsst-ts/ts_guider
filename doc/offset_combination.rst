.. py:currentmodule:: lsst.ts.guider.pipeline

.. _offset-combination:

Combining guider offsets
========================================

``MultiSensorRunner`` coordinates one ``SensorTracker`` per sensor and uses
``OffsetCombiner`` to produce a translation for each acquisition.
``run_offline`` drives the same calculation from one FITS sequence per sensor.

From amplifier pixels to camera pixels
----------------------------------------

For each measurement that passes quality cuts and has a reference,
``OffsetCombiner`` obtains the local displacement with
``measurement.offset_from(reference)``: measured position minus reference.
``GuiderOrientation`` then applies the amplifier's raw X/Y flips followed
by the detector's quarter-turn rotation, read from the LSST camera model.
Only the vector is transformed; translations cancel when two positions
are subtracted.
All offsets remain in pixels, expressed along the common camera axes.

For example, for amplifier C05 in the camera model:

.. list-table:: Two sensors measuring the same camera displacement
   :header-rows: 1
   :widths: 25 35 40

   * - Sensor
     - Local amplifier offset
     - Camera offset
   * - R00_SG0
     - ``(1, -2)``
     - ``(2, -1)``
   * - R04_SG0
     - ``(2, -1)``
     - ``(2, -1)``

The combined offset is the unweighted arithmetic mean of the accepted
camera offsets in that acquisition.
It describes translation, without fitting camera rotation, applying a
temporal average, or converting to angular units.
For each axis, ``scatter`` is the sample standard deviation across sensors
(``ddof=1``), and ``error`` is ``scatter / sqrt(n_valid)``.
The latter estimates uncertainty on the mean from sensor disagreement;
it does not propagate individual centroid-fit errors.

The returned ``CombinedOffset`` retains:

* ``stamp_index`` and the mean ``combined_dx``, ``combined_dy``.
* ``scatter_dx``, ``scatter_dy``, ``error_dx``, and ``error_dy``.
* ``n_valid``, the number of offsets used, and ``n_total``, the number of
  supplied measurements.
* ``per_sensor_offset``, the offsets actually included after transformation.
* ``measurements``, a copy of the supplied measurement dictionary,
  including quality failures, for diagnostics and later publication.
  Its centroids remain in the original amplifier coordinates.

With one accepted sensor the offset is returned, but scatter and error
are NaN. With none, the combined offset, scatter, and error are NaN,
while measurements remain available.
The runner skips unknown sensors and sensors without a locked reference;
they do not contribute to ``n_total``.

Camera metadata requirements
----------------------------------------

To obtain a camera-coordinate result, provide a valid camera detector name
and amplifier for every contributing sensor.
For FITS inputs these come from ``RAFTBAY``, ``CCDSLOT``, and ``ROISEG``;
for direct ``MultiSensorRunner`` use, supply ``sensor_amplifiers``.

The original combiner's fallback behavior is retained: a sensor absent
from the amplifier map is averaged in its raw amplifier coordinates.
A camera lookup that raises ``LookupError`` also falls back, with a warning.
Other exception types propagate; for example, an invalid amplifier in the
LSST camera model can raise ``InvalidParameterError``.
An incomplete map can therefore mix coordinate frames, so its result
must not be interpreted as a camera offset.
An entirely absent map is useful only when the supplied offsets already
share an orientation.

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
the sequences. Duplicate sensor names replace an earlier file in the
input mapping.
The runner locks references using the configured initial seed frames and
then processes all indices, including those seed frames.
It stops at the shortest input sequence, including any sequence whose
reference failed to lock. If no reference locks, it returns an empty list.
Files are read into memory before processing.

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
writes compressed FITS sequences with known camera displacements, and
runs the complete offline path against the real camera model.
It checks integer and fractional motion within 0.05 pixel for this
specific source/noise setup, seed-frame results, shortest-sequence
handling, and retained rejected measurements.
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
