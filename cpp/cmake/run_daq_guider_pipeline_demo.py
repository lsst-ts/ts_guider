"""End-to-end guider pipeline over the DAQ/SDK stamp source.

This wires the tracking pipeline (``lsst.ts.guider.pipeline``) to the
live DAQ/GDS stamp stream exposed by ``guiderGDS.DaqStampSource``. It
runs the same ``StreamingGuiderProcessor`` (``SensorTracker`` /
``OffsetCombiner``) the CSC uses, fed one stamp at a time from the C++
worker thread, plus timing metrics on the real-time-critical path
(per-stamp HSM measurement) and the cheaper post-stream combine.

Subscribes to a GDS partition exactly like ``run_daq_stamp_source_demo.py``.
Requires the 3-terminal emulator setup documented in that file
(dsid_standalone, this demo, gds_emulator), the readline-shim, and the
UDP buffer tuning.

Streaming model
---------------
Stamps arrive asynchronously as ``on_stamp(pixels, metadata)`` keyed by
``metadata.sensor_index`` and ``metadata.stamp_index``. Each sensor
buffers its first ``seed_frames`` stamps, locks a reference, then
measures every stamp (the buffered seed frames are measured at lock
time so coverage includes the warm-up stamps). Measurements are grouped by
``stamp_index``; once a sensor is locked, each acquisition (one
``stamp_index`` across sensors) is combined live as soon as the next
live one starts, so pass ``--per-frame`` to log the combined offset
frame by frame as the stream runs. The seed warm-up frames and the
final open frame are combined in ``finalize()`` (they do not appear in
the live ``--per-frame`` trace, only in the end-of-run report).

A change in ``metadata.sequence`` marks a new pointing: the processor
ends the finished visit and re-acquires on the new field's guide stars.
The demo prints a per-visit summary at each boundary (and for the last
visit at ``finalize()``).

Sensor orientation
------------------
Centroiding stays in amplifier (ROI) coordinates. Before the combine
averages the per-sensor offsets, each is rotated into the common camera
frame (amplifier flip + detector ``nQuarter``) using the per-sensor
amplifier map. The map is built automatically from the per-stamp
``StampMetadata.segment`` the first time each sensor is seen (see
``StreamingGuiderProcessor._record_amplifier``), so the live combine
runs in the camera frame with no extra configuration.

Run (inside the container)
---------------------------
::

    source /opt/lsst/software/stack/loadLSST.bash; setup lsst_distrib
    setup -k -r /home/saluser/ts_repos/ts_guider  # puts the package on path
    export DAQ_SDK=/home/saluser/ts_repos/R5-V13.16
    export LD_LIBRARY_PATH=~/readline-shim:$DAQ_SDK/x86/lib:$LD_LIBRARY_PATH
    cd /home/saluser/ts_repos/ts_guider/cpp/cmake
    PYTHONPATH=build_v16:/home/saluser/ts_repos/ts_guider/python:$PYTHONPATH \\
        python run_daq_guider_pipeline_demo.py \\
      --partition gds-emu --max-stamps 0 --timeout 0 --debug
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Callable
from time import perf_counter

import numpy as np
from lsst.ts.guider.pipeline import (
    CombinedOffset,
    GuiderTrackerConfig,
    StreamingGuiderProcessor,
)

log = logging.getLogger("guider_pipeline")


def make_combined_offset_logger(
    processor: StreamingGuiderProcessor,
) -> Callable[[CombinedOffset], None]:
    """Build the ``on_combined_offset`` callback for ``--per-frame``.

    The returned callback logs one acquisition's combined offset as
    soon as it is ready, so the combined dx/dy (the per-frame average
    over the locked sensors, each ``+/-`` its standard error) appears
    live, frame by frame, while the stream runs. The trailing time is
    that frame's combine cost in milliseconds, read from
    ``metrics.combine_times``: its last entry is appended right before
    this callback fires (see
    ``StreamingGuiderProcessor._combine_acquisition``).
    """

    def log_combined_offset(combined: CombinedOffset) -> None:
        combine_ms = processor.metrics.combine_times[-1] * 1e3
        log.info(
            "%6d | dx=%+.3f +/- %.3f | dy=%+.3f +/- %.3f | "
            "scatter %.3f,%.3f | %d/%d | combine %.3f ms",
            combined.stamp_index,
            combined.combined_dx,
            combined.error_dx,
            combined.combined_dy,
            combined.error_dy,
            combined.scatter_dx,
            combined.scatter_dy,
            combined.n_valid,
            combined.n_total,
            combine_ms,
        )

    return log_combined_offset


def log_visit_start(sequence: int | None, label: str) -> None:
    """Log a banner at the first stamp of each visit.

    Registered as ``StreamingGuiderProcessor.on_visit_start`` when
    ``--per-frame`` is set, so it fires once per pointing before that
    visit's live per-frame trace, making it unambiguous which
    sequence/image the following rows belong to.
    """
    log.info("visit start: sequence %s image '%s'", sequence, label or "?")


def log_visit_summary(sequence: int | None, offsets: list[CombinedOffset]) -> None:
    """Log a per-visit summary when a pointing ends.

    Registered as ``StreamingGuiderProcessor.on_visit_complete``, so it
    fires at each sequence boundary (telescope slew) and once more for
    the final visit at ``finalize()``.
    """
    valid = [offset for offset in offsets if offset.n_valid > 0]
    if not valid:
        log.info("visit (sequence %s) complete: no valid offsets", sequence)
        return
    dx = np.array([offset.combined_dx for offset in valid])
    dy = np.array([offset.combined_dy for offset in valid])
    error_dx = np.array([offset.error_dx for offset in valid])
    error_dy = np.array([offset.error_dy for offset in valid])
    log.info(
        "visit (sequence %s) complete\n"
        "acquisitions : %d/%d with >=1 valid sensor\n"
        "overall dx   : %+.3f +/- %.3f px\n"
        "overall dy   : %+.3f +/- %.3f px",
        sequence,
        len(valid),
        len(offsets),
        np.median(dx),
        np.nanmedian(error_dx),
        np.median(dy),
        np.nanmedian(error_dy),
    )


def run_live(
    processor: StreamingGuiderProcessor,
    partition: str,
    max_stamps: int,
    timeout_seconds: float,
) -> None:
    """Subscribe to a live GDS partition and stream until done.

    ``max_stamps <= 0`` runs without a stamp-count limit and
    ``timeout_seconds <= 0`` waits indefinitely; in either continuous
    case stop with Ctrl-C, which still triggers the final combine and
    report. The stream is polled in short slices so the interrupt is
    handled promptly.
    """
    import guiderGDS

    source = guiderGDS.DaqStampSource(
        partition=partition,
        locations=guiderGDS.LocationSet.any(),
    )
    done = threading.Event()

    def on_stamp(pixels, metadata):
        processor.on_stamp(pixels, metadata)
        if max_stamps > 0 and processor.metrics.total_stamps >= max_stamps:
            done.set()

    deadline = None if timeout_seconds <= 0 else perf_counter() + timeout_seconds
    source.start_stamp_stream(on_stamp)
    try:
        while not done.wait(0.5):
            if deadline is not None and perf_counter() >= deadline:
                log.warning(
                    "Timeout: received %d stamps.",
                    processor.metrics.total_stamps,
                )
                break
    except KeyboardInterrupt:
        log.info(
            "Interrupted; stopping after %d stamps.",
            processor.metrics.total_stamps,
        )
    finally:
        source.stop_stamp_stream()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--partition", default="gds-emu")
    parser.add_argument(
        "--max-stamps",
        type=int,
        default=2000,
        help="Stop after this many stamps; 0 runs without a limit.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=100.0,
        help="Seconds to wait for stamps; 0 waits forever (stop with Ctrl-C).",
    )
    parser.add_argument(
        "--seed-frames",
        type=int,
        default=GuiderTrackerConfig.seed_frames,
        help="Warm-up stamps buffered per sensor before locking a "
        "reference; the single default lives on GuiderTrackerConfig. "
        "Lower it (e.g. 10 or 5) for short visits.",
    )
    parser.add_argument("--min-snr", type=float, default=10.0)
    parser.add_argument(
        "--progress",
        type=int,
        default=0,
        metavar="N",
        help="Log a running summary every N stamps; 0 disables it.",
    )
    parser.add_argument(
        "--per-frame",
        action="store_true",
        help="Log the combined offset for every acquisition (frame) "
        "live, as the stream runs, not just the final run summary.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Log every measured stamp (centroid, snr, pass/reject).",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.debug:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.WARNING
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = GuiderTrackerConfig(seed_frames=args.seed_frames, min_snr=args.min_snr)
    processor = StreamingGuiderProcessor(
        config,
        progress_interval=args.progress,
        on_visit_start=log_visit_start if args.per_frame else None,
        on_visit_complete=log_visit_summary,
    )
    if args.per_frame:
        processor.on_combined_offset = make_combined_offset_logger(processor)

    run_live(processor, args.partition, args.max_stamps, args.timeout)

    processor.finalize()
    processor.report()
    return 0 if processor.combined_offsets else 1


if __name__ == "__main__":
    sys.exit(main())
