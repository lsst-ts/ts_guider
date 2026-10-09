.. _sensor-identifiers:

DAQ sensor and amplifier identifiers
========================================

Each DAQ stamp carries integer identifiers for its detector and ROI
amplifier. The functions in ``lsst.ts.guider.sensor_identifiers`` convert
these values into names used by the camera model, such as ``R40_SG0``
and ``C05``.

Decoding the sensor index
----------------------------------------

LSSTCam has a 5-by-5 grid of raft bays, named R00 through R44 by row and
column. Each corner raft has two guide sensors, SG0 and SG1, for eight
guide sensors in total::

            col 0   col 1   col 2   col 3   col 4
    row 0   [R00]   R01     R02     R03     [R04]
    row 1    R10    R11     R12     R13      R14
    row 2    R20    R21     R22     R23      R24
    row 3    R30    R31     R32     R33      R34
    row 4   [R40]   R41     R42     R43     [R44]

The GDS SDK identifies a sensor by its bay, board, and sensor slot.
The bay number is ``row * 5 + column``, from 0 through 24. Each bay has
three board slots, with three sensor slots per board. Guide sensors use
board 1; board 0 is for science sensors and board 2 for wavefront sensors.

``Location.index()`` combines these three numbers into the integer
exposed as ``StampMetadata.sensor_index``::

    sensor_index = bay * 9 + board * 3 + sensor

For example, R40_SG0 is in bay 20, board 1, sensor slot 0, so its index
is ``20 * 9 + 1 * 3 + 0 = 183``. The SDK writes its location as
``40/1/0``: the first part gives the raft row and column, not the linear
bay number.

.. list-table:: Guide sensor identities
   :header-rows: 1
   :widths: 25 25 25 25

   * - Detector name
     - SDK location
     - Bay number
     - Sensor index
   * - R00_SG0
     - 00/1/0
     - 0
     - 3
   * - R00_SG1
     - 00/1/1
     - 0
     - 4
   * - R04_SG0
     - 04/1/0
     - 4
     - 39
   * - R04_SG1
     - 04/1/1
     - 4
     - 40
   * - R40_SG0
     - 40/1/0
     - 20
     - 183
   * - R40_SG1
     - 40/1/1
     - 20
     - 184
   * - R44_SG0
     - 44/1/0
     - 24
     - 219
   * - R44_SG1
     - 44/1/1
     - 24
     - 220

Use ``detector_name_from_sensor_index`` to recover the detector name:

.. code-block:: python

   from lsst.ts.guider.sensor_identifiers import detector_name_from_sensor_index

   assert detector_name_from_sensor_index(183) == "R40_SG0"
   assert detector_name_from_sensor_index(184) == "R40_SG1"

The function raises ``ValueError`` if the index identifies a board other
than board 1. It does not check whether the bay and sensor slot contain
a physical guide sensor.

Identifying the amplifier
----------------------------------------

``StampMetadata.segment`` identifies the amplifier containing the ROI.
It is separate from ``sensor_index``: the detector name identifies the
sensor, and the amplifier name identifies the part of that sensor being
read out.

DAQ segment numbers follow the amplifier labels: 0--7 and 10--17.
``amplifier_name_from_segment_index`` adds ``C`` and formats the number
with two digits:

.. code-block:: python

   from lsst.ts.guider.sensor_identifiers import amplifier_name_from_segment_index

   assert amplifier_name_from_segment_index(5) == "C05"
   assert amplifier_name_from_segment_index(17) == "C17"

In a FITS sequence, the corresponding ``ROISEG`` header value is a string
such as ``Segment05``. The live DAQ metadata supplies the integer ``5``.

Neither helper looks up names in the camera model. ``OffsetCombiner``
uses the detector and amplifier names to obtain the flips and rotation
needed to express offsets in camera coordinates. The names must exist
in that model; see :ref:`offset-combination`.

API reference
----------------------------------------

.. automodapi:: lsst.ts.guider.sensor_identifiers
   :no-main-docstr:
   :no-inheritance-diagram:
