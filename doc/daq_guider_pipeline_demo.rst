.. _daq-guider-pipeline-demo:

DAQ guider pipeline demo
========================================

The ``run_daq_guider_pipeline_demo`` command receives DAQ stamps, tracks
each sensor's reference star, and reports combined offsets in camera
pixels. It connects ``guiderGDS.DaqStampSource`` to
``StreamingGuiderProcessor``, using the same tracker and combiner as the
offline runner. It is a standalone development tool; it does not publish
guiding commands through the CSC.

Use :ref:`guider-installation` to install the package, build the binding,
and prepare the container and SDK environment. The
:ref:`stamp-source demo <daq-stamp-source-demo>` is a useful first check
that stamps reach Python. For the processing steps and call flow, see
:ref:`streaming-guider-pipeline`.

Three-terminal run
----------------------------------------

Use three shells in the prepared development container. Each needs the
activated LSST environment, SDK executables and libraries on its paths,
and this checkout's Python package and compiled binding available.

In terminal 1, start a local test partition service. Omit this command
when the partition already has a service::

    dsid_standalone -p gds-emu

In terminal 2, start the subscriber before publishing any stamps::

    run_daq_guider_pipeline_demo --partition gds-emu \
        --seed-frames 5 --max-stamps 0 --timeout 0 --per-frame

This example disables both automatic stopping limits so that it can
receive both emulator passes below. Stop it with Ctrl-C after the emulator
finishes. For a timed run, replace ``--timeout 0`` with a duration in
seconds that allows time to start the emulator and receive its data.
The camera model is constructed before subscription, keeping that initial
setup out of stamp processing.

In terminal 3, publish matching guider FITS sequences::

    gds_emulator -q 1234 -l 2 gds-emu 600 \
        /path/to/MC_O_20260429_000027_R00_SG0_guider.fits \
        /path/to/MC_O_20260429_000027_R04_SG0_guider.fits

The R5-V13.16 emulator interprets ``600`` as milliseconds between stamps
from each sensor. ``-q 1234`` selects the first DAQ sequence number, and
``-l 2`` repeats the files twice. The second pass advances the sequence
number and resets the stamp counter. Add the other six guider files to
exercise all eight sensors.

Use files from the same acquisition sequence, with matching stamp counts
and dimensions. They need a metadata-only primary HDU with the guider ROI,
timing, and hardware headers expected by the SDK, followed by compressed
image extensions. See :ref:`offset-combination` for the guider FITS layout.
An arbitrary FITS cube is not an equivalent emulator input.

With ``--per-frame``, the first received stamp of each series produces a
start message. On the first stamp with a new sequence number, the processor
finishes the preceding series and starts reference selection again. It
uses the observation label when available, otherwise the series label
carried by START metadata.

The emulator finishing does not stop the subscriber. In terminal 2, press
Ctrl-C to stop and join the DAQ worker, combine the remaining acquisitions,
and report the last series and the complete run. The subscriber does not
stop the partition service or emulator; close those processes in their
own terminals when they are no longer needed.

Command options
----------------------------------------

Run ``run_daq_guider_pipeline_demo --help`` for the current options.
Requesting help does not require the binding or open a DAQ stream.

.. list-table::
   :header-rows: 1
   :widths: 24 14 62

   * - Option
     - Default
     - Meaning
   * - ``--partition``
     - ``gds-emu``
     - DAQ partition to subscribe to.
   * - ``--max-stamps``
     - ``2000``
     - Stop after this many individual sensor stamps across the run.
       Zero removes the count limit.
   * - ``--timeout``
     - ``100``
     - Run duration in seconds, starting just before the source starts.
       Zero removes the deadline.
   * - ``--seed-frames``
     - ``10``
     - Images per sensor before its first reference-selection attempt.
       If that attempt fails, retry with each additional image.
   * - ``--min-snr``
     - ``10``
     - Minimum signal-to-noise ratio for an accepted measurement.
   * - ``--progress N``
     - ``0``
     - Log counts every N received stamps. Zero disables progress logging.
   * - ``--per-frame``
     - Off
     - Log series starts and combined acquisitions as live results become
       available. Acquisitions combined at series completion appear in
       summaries instead.
   * - ``--log-level``
     - ``INFO``
     - Select ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, or ``CRITICAL``.
       Names are case-insensitive.

Stamp and progress limits must be nonnegative integers; the seed threshold
must be a positive integer. Timeout and SNR values must be finite and
nonnegative. The run ends when either enabled limit is reached. A stamp limit
counts each sensor separately: one acquisition from eight sensors adds
eight stamps, but produces at most one combined offset.

Reports, progress, and the live trace use ``INFO`` and are written to
standard error. ``DEBUG`` also includes individual sensor measurements.
For example, ``--log-level WARNING`` suppresses reports and keeps warnings
and errors, including timeout messages.

Reading the results
----------------------------------------

The live trace identifies each acquisition by its stamp index and reports
``dx`` and ``dy`` in camera pixels, their standard errors, sensor scatter,
the accepted/returned sensor counts, and combination time in milliseconds.
The returned count includes rejected measurements; it is not the number
of sensors expected from the DAQ.

Reference selection initially buffers stamps. Once a sensor selects a
reference, it measures those buffered images as well as subsequent arrivals.
These earlier measurements can contribute to pending acquisitions, but
cannot change an acquisition already combined. The live trace therefore
does not list every result in the final report: buffered acquisitions and
the final open acquisition can be combined when the series ends. See
:ref:`streaming-guider-pipeline` for a worked example.

Each completed series has a summary, even without ``--per-frame``. The
final report covers all series in the run and includes:

* Stamp, measurement, and combined-acquisition counts. Discarded seed
  measurements were returned after their acquisition had already closed;
  they are also included in the accepted or rejected measurement counts.
* Reference-selection, measurement, stamp-callback, and combination
  durations, summarized as count, mean, median, 95th percentile, and
  maximum in milliseconds.
* Offset medians and variation over time, sensor disagreement, contributing
  sensor counts, and the last five acquisitions with accepted measurements.

The locked-sensor list and automatic amplifier map describe the final
series. Counts and timing samples accumulate throughout the run.

Offset medians describe displacement across acquisitions. The report also
takes the median of the individual acquisitions' standard errors; that
number is not the uncertainty of the offset median. These statistics use
acquisitions with at least one accepted sensor measurement, and the error
summary uses only finite errors. A single contributing sensor gives a
usable offset but no estimate of sensor scatter or standard error. If
none of the acquisitions has a finite error, the report shows ``NaN``.
See :ref:`offset-combination` for the combination and error definitions.

Processing durations measure work inside the program. They do not measure
the delay from DAQ acquisition to result delivery. Measurement and
combination can run inside a stamp callback, so their times overlap with
the callback duration and should not be added to it. The reported stamp
rate uses elapsed time between received stamps, including pauses in
delivery; it is not a measurement of maximum processing throughput.

Stopping and failures
----------------------------------------

A count limit, timeout, or Ctrl-C during streaming is a normal stop. After
the source stops and its worker joins, the demo finalizes the processor
and reports the results. A count limit can stop partway through an
acquisition; combination uses the measurements available then.

The command returns status ``0`` if at least one acquisition was combined,
or ``1`` if none was combined. Status ``0`` does not guarantee an accepted
offset: a combined acquisition can contain only rejected measurements.
Invalid command-line arguments return status ``2``.

A processing exception is passed back to the main thread after stopping
the source. Unexpected processing or shutdown failures propagate without
finalization or a normal run report. If importing ``guiderGDS`` fails,
the error points to the binding build and environment setup described in
:ref:`guider-installation`.

Keep development runs bounded. Results and timing samples accumulate in
memory, and each sensor retains its seed images until it selects a
reference or the series ends. The :ref:`installation guide
<guider-installation>` also describes the available Python and binding
checks and the environment needed to validate the decoder with DAQ traffic.

API reference
----------------------------------------

.. autofunction:: lsst.ts.guider.demo.run_daq_guider_pipeline_demo.run_daq_guider_pipeline_demo
