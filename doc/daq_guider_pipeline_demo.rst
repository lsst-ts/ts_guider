.. _daq-guider-pipeline-demo:

DAQ guider pipeline demo
========================================

``run_daq_guider_pipeline_demo`` connects the shared streaming processor
to ``guiderGDS.DaqStampSource`` and reports combined camera offsets.
It uses the same ``SensorTracker`` and ``OffsetCombiner`` as the offline
runner. See :ref:`streaming-guider-pipeline` for acquisition and series rules.

Build and activate the package and extension using :ref:`guider-installation`.
The demo constructs the camera before subscribing so the first transform
does not stall stamp handling or discovery of the next series.
Each terminal below needs the activated LSST environment, SDK binaries and
libraries on its paths, and the CMake build directory on ``PYTHONPATH``.

Three-terminal run
----------------------------------------

Terminal 1 starts a local test partition service::

    dsid_standalone -p gds-emu

Omit that step when the partition already has a service.
Terminal 2 starts the subscriber before the emulator::

    run_daq_guider_pipeline_demo --partition gds-emu \
        --seed-frames 5 --max-stamps 0 --timeout 0 --per-frame

Terminal 3 publishes aligned guider FITS sequences::

    gds_emulator -q 1234 -l 2 gds-emu 600 \
        /path/to/MC_O_20260429_000027_R00_SG0_guider.fits \
        /path/to/MC_O_20260429_000027_R04_SG0_guider.fits

Add the other six guider files to exercise all eight sensors.
The files need metadata-only primary HDUs followed by compressed image
extensions, as described under :ref:`offset-combination`.
The SDK additionally uses ROI dimensions, timing, origin, and hardware
metadata in the primary header; the integration test provides a complete
synthetic example.

``-l 2`` runs two passes with a new START sequence and reset stamp counter
on the second pass. The processor should print a start banner for each
series and a completion summary when the following series begins.
Stop the subscriber with Ctrl-C after the emulator finishes to finalize
and report the last series.

The R5-V13.16 FITS emulator path sends START, STAMP, and STOP but does not
provide a RESUME event. On that path ``obs_id`` stays empty, and the
processor uses ``series_id`` as its label. The emulator's ``-c`` option
overrides comments on RESUME events when its input supplies them;
it does not create a RESUME for a FITS sequence.

Command options and output
----------------------------------------

* ``--max-stamps`` defaults to 2000; zero removes the count limit.
* ``--timeout`` defaults to 100 seconds; zero waits until interrupted
  or the count limit is reached.
* ``--seed-frames`` uses the tracker default unless explicitly supplied.
* ``--min-snr`` sets the measurement acceptance threshold (default 10).
* ``--progress N`` logs counts every N arrivals; zero disables it.
* ``--per-frame`` logs live combined offsets, their standard errors,
  sensor scatter, contributing counts, and combination cost in milliseconds.
* ``--debug`` additionally logs individual measurements; ``--quiet``
  limits log messages to warnings and errors.

Warm-up acquisitions and the final open acquisition appear in the series
summary and final report rather than the live callback trace.
The final report includes measurement/lock/callback/combine timings and
the number of seed measurements discarded after their acquisition closed.
It reports a median offset over the run for inspection; that summary is
not a separate guiding command or a fitted camera rotation.

Timeout and interruption stop and join the source before finalization.
A Python processing exception is returned to the main thread after the
source stops, rather than being only printed by the binding callback.
An empty run returns exit code 1; a run with combined results returns 0.

Automated validation
----------------------------------------

Ordinary streaming tests use synthetic arrivals, without a network source::

    python -m pytest -q tests/test_streaming_guider_processor.py \
        tests/test_sensor_identifiers.py tests/test_daq_guider_pipeline_demo.py

Binding tests require the built ``guiderGDS`` extension::

    python -m pytest -q tests/test_guiderGDS_bindings.py

The optional end-to-end test generates two compressed FITS sequences with
known displacements and starts its own temporary partition service.
It runs two emulator loops through sequence rollover (65535 to 0), then
publishes a third series with changed ROI metadata.
It checks decoded pixel values, sensor-specific metadata, reference
resets, offsets, and clean finalization. Enable it explicitly in the
configured development environment::

    export GUIDER_RUN_EMULATOR=1
    export GUIDER_DECODER_TEST="$PWD/cpp/cmake/build/tests/test_decoder"
    python -m pytest -q tests/test_daq_emulator.py

The default test interface is ``lsst-daq``; ``GUIDER_EMULATOR_INTERFACE``
overrides the partition-service interface when the SDK is configured
accordingly. The test stops only the service and emulator it creates.
It does not create interfaces or change kernel buffers.

``test_decoder`` drives the actual C++ decoder's SDK callbacks to verify
that sensor metadata remains independent, a new START clears the previous
image, and RESUME updates the image without changing the ROI series.
It uses the integration test's partition; outside that test, set
``GUIDER_TEST_PARTITION`` to an existing test partition before running
the executable. The SDK-free C++ suite remains available with
``BUILD_MODULE=OFF``.

API reference
----------------------------------------

.. autofunction:: lsst.ts.guider.demo.run_daq_guider_pipeline_demo
