.. py:currentmodule:: lsst.ts.guider.pipeline

.. _offset-combination:

Combining guider offsets
========================================

``OffsetCombiner`` combines accepted sensor displacements into one
translation for each acquisition, retaining the individual measurements
in ``CombinedOffset``.

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

Camera metadata requirements
----------------------------------------

To obtain a camera-coordinate result, provide a valid camera detector name
and amplifier for every contributing sensor.
Supply this information through the combiner's ``sensor_amplifiers`` map.
``build_sensor_amplifiers`` builds that map from the segment strings
in ``GuiderSequence`` objects, using the FITS ``ROISEG`` convention.
Each sequence holds one sensor's stamp cube, timestamps, name, and segment.

The original combiner's fallback behavior is retained: a sensor absent
from the amplifier map is averaged in its raw amplifier coordinates.
A camera lookup that raises ``LookupError`` also falls back, with a warning.
Other exception types propagate; for example, an invalid amplifier in the
LSST camera model can raise ``InvalidParameterError``.
An incomplete map can therefore mix coordinate frames, so its result
must not be interpreted as a camera offset.
An entirely absent map is useful only when the supplied offsets already
share an orientation.

Use the standard LSST environment with ``lsst.obs.lsst`` available for the
camera model, along with the package's tracking and CSC dependencies.

Reproducible validation
----------------------------------------

``tests/test_offset_combiner.py`` checks camera-coordinate means, sample
scatter, standard errors, and retained measurements using the real camera
model. It covers zero and one accepted sensor, quality rejection, missing
references, amplifier-map construction, and the original unmapped mode.
The orientation tests cover amplifier flips and all four quarter turns,
real guider camera entries, and FITS segment-name parsing.

.. code-block:: bash

   python -m pytest -q tests/test_guider_orientation.py tests/test_offset_combiner.py

The implementation follows OSW-2858: ``sensor_orientation.py`` becomes
``guider_orientation.py``, and ``GuiderSequence`` and ``CombinedOffset``
move from ``models.py`` into matching modules.
The combiner reuses the existing ``CentroidMeasurement.offset_from``
subtraction. DAQ identity helpers and streaming orchestration belong to
the subsequent live-processing stage.

API reference
----------------------------------------

The :ref:`sensor-tracking` API reference includes ``GuiderSequence``,
``CombinedOffset``, ``OffsetCombiner``, and ``build_sensor_amplifiers``.
The camera helpers are documented below.

.. automodapi:: lsst.ts.guider.guider_orientation
   :no-main-docstr:
   :no-inheritance-diagram:
