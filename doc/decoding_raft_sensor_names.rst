.. _sensor-identifiers:

Decoding raft and sensor names
========================================

LSSTCam has a 5-by-5 grid of raft bays, named R00 through R44 (row/column).
Each corner raft has two guide sensors, SG0 and SG1, for eight total::

            col 0   col 1   col 2   col 3   col 4
    row 0   [R00]   R01     R02     R03     [R04]
    row 1    R10    R11     R12     R13      R14
    row 2    R20    R21     R22     R23      R24
    row 3    R30    R31     R32     R33      R34
    row 4   [R40]   R41     R42     R43     [R44]

GDS location packing
----------------------------------------

The SDK's ``Location`` represents a bay/board/sensor triple.
The bay is ``row * 5 + column`` (0 through 24), there are three boards
per bay, and three sensor slots per board. In a corner raft, guide sensors
occupy board 1; board 0 is the science board and board 2 the wavefront board.
``Location.index()`` packs these fields into a single integer::

    index = bay * 9 + board * 3 + sensor

The value is exposed as ``StampMetadata.sensor_index``.
The SDK location string prints the bay using raft row/column naming,
so ``40/1/0`` corresponds to linear bay 20, not bay 40.

.. list-table:: Guide sensor identities
   :header-rows: 1
   :widths: 25 25 25 25

   * - Camera / FITS name
     - SDK location
     - Linear bay
     - Packed index
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

``detector_name_from_sensor_index`` converts the packed index into the
camera name without importing a camera model. The original helper checks
the board number; it is not a complete validator of all physical sensor
locations. For example::

    from lsst.ts.guider.sensor_identifiers import detector_name_from_sensor_index

    assert detector_name_from_sensor_index(183) == "R40_SG0"
    assert detector_name_from_sensor_index(184) == "R40_SG1"

Amplifiers and acquisition identity
----------------------------------------

The ROI amplifier is a separate field, ``StampMetadata.segment``.
``amplifier_name_from_segment_index(5)`` returns ``C05``; segment 17
returns ``C17``. These are amplifier labels (00--07 and 10--17), rather
than a flat range 0--15. The FITS equivalent is ``ROISEG=Segment05``.

All sensors with the same series sequence and acquisition index contribute
to one combined offset. The index alone is insufficient across series.

API reference
----------------------------------------

.. automodapi:: lsst.ts.guider.sensor_identifiers
   :no-main-docstr:
   :no-inheritance-diagram:
