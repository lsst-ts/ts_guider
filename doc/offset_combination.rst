.. py:currentmodule:: lsst.ts.guider.guider_orientation

.. _offset-combination:

Camera-coordinate guider offsets
========================================

Guider measurements begin in amplifier pixel coordinates.
Before offsets from different sensors can be combined, they must be
expressed along common camera axes.

From amplifier pixels to camera pixels
----------------------------------------

For a measurement that passes quality cuts and has a reference,
``measurement.offset_from(reference)`` gives the local displacement:
measured position minus reference.
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

Camera metadata requirements
----------------------------------------

Provide a valid camera detector name and amplifier for each transform.
The ``amplifier_name_from_segment`` helper converts a FITS ``ROISEG``
value such as ``Segment05`` to amplifier name ``C05``.
``generate_orientation_table`` exposes the guide sensors' rotations
and amplifier flips for inspection.

Use the standard LSST environment with ``lsst.obs.lsst`` available for the
camera model, along with the package's tracking and CSC dependencies.

Reproducible validation
----------------------------------------

``tests/test_guider_orientation.py`` checks amplifier flips and all four
quarter turns by comparing transformed displacement vectors with the
motion of labeled pixels in transformed images.
It also checks real guider camera entries and FITS segment-name parsing.

.. code-block:: bash

   python -m pytest -q tests/test_guider_orientation.py

The implementation follows the camera-transform portion of OSW-2858's
``sensor_orientation.py``, now named ``guider_orientation.py`` to match
the ``GuiderOrientation`` class.
DAQ identity helpers belong to the subsequent live-processing stage.

API reference
----------------------------------------

.. automodapi:: lsst.ts.guider.guider_orientation
   :no-main-docstr:
   :no-inheritance-diagram:
