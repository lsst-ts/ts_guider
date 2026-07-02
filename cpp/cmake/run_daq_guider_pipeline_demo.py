"""End-to-end guider pipeline over the DAQ/SDK stamp source.

This wires the tracking pipeline (``lsst.ts.guider.pipeline``) to the
live DAQ/GDS stamp stream exposed by ``guiderGDS.DaqStampSource``. It is
the intermediate step between the standalone FITS analysis
(``ts_guider/scripts/guider_offline.py``) and the CSC: the *same*
``StreamingGuiderProcessor`` (``SensorTracker`` / ``OffsetCombiner``)
algorithm, now fed one stamp at a time from the C++ worker thread, plus
timing metrics on the real-time-critical path (per-stamp HSM
measurement) and the cheaper post-stream combine.

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
time so coverage matches the offline run). Measurements are grouped by
``stamp_index``; once a sensor is locked, each acquisition (one
``stamp_index`` across sensors) is combined live as soon as the next
live one starts, so pass ``--per-frame`` to print the combined offset
frame by frame as the stream runs. The seed warm-up frames and the
final open frame are combined in ``finalize()`` (they do not appear in
the live ``--per-frame`` trace, only in the end-of-run report).

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
from time import perf_counter

from lsst.ts.guider.pipeline import (
    CombinedOffset,
    GuiderTrackerConfig,
    StreamingGuiderProcessor,
)

log = logging.getLogger("guider_pipeline")


def print_combined_offset(combined: CombinedOffset) -> None:
    """Print one acquisition's combined offset as soon as it is ready.

    Registered as ``StreamingGuiderProcessor.on_combined_offset`` when
    ``--per-frame`` is set, so this runs live, frame by frame, while
    the stream is still running (see the "Streaming model" section in
    the module docstring).
    """
    print(
        f"{combined.stamp_index:6d} | "
        f"dx={combined.combined_dx:+.3f} +/- {combined.error_dx:.3f} | "
        f"dy={combined.combined_dy:+.3f} +/- {combined.error_dy:.3f} | "
        f"scatter {combined.scatter_dx:.3f},{combined.scatter_dy:.3f} | "
        f"{combined.n_valid}/{combined.n_total}"
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
    parser.add_argument("--seed-frames", type=int, default=30)
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
        help="Print the combined offset for every acquisition (frame) "
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
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    config = GuiderTrackerConfig(seed_frames=args.seed_frames, min_snr=args.min_snr)
    processor = StreamingGuiderProcessor(
        config,
        progress_interval=args.progress,
        on_combined_offset=print_combined_offset if args.per_frame else None,
    )
    if args.per_frame:
        print("   idx |        dx +/- err |        dy +/- err | scatter | sensors")

    run_live(processor, args.partition, args.max_stamps, args.timeout)

    processor.finalize()
    processor.report()
    return 0 if processor.combined_offsets else 1


if __name__ == "__main__":
    sys.exit(main())
