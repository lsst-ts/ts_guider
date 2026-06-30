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
``stamp_index``; the per-acquisition combine runs after the stream
stops, which keeps it independent of stamp delivery order.

Sensor orientation
------------------
Centroiding stays in amplifier (ROI) coordinates. Before the combine
averages the per-sensor offsets, each is rotated into the common camera
frame (amplifier flip + detector ``nQuarter``) using the per-sensor
amplifier map. The per-stamp ``StampMetadata`` now carries the
``segment``, so the amplifier map can be populated from it; until
``run_live`` wires that (see the TODO there) the live combine falls
back to amplifier coordinates.

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
    GuiderTrackerConfig,
    StreamingGuiderProcessor,
)

log = logging.getLogger("guider_pipeline")


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

    # TODO: populate the per-sensor amplifier map for the camera-frame
    # combine from the per-stamp metadata.segment (now exposed by the
    # binding) via processor.set_sensor_amplifiers(). The flip and
    # rotation are applied together, so without the map no camera-frame
    # transform is applied and the combine averages in amplifier
    # coordinates.
    if not processor.combiner.sensor_amplifiers:
        log.warning(
            "Live combine has no per-sensor amplifier map (ROI segment); "
            "offsets are averaged in amplifier coordinates without the "
            "camera-frame transform. Sensor names are decoded so the "
            "transform activates once the map is set."
        )

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
    processor = StreamingGuiderProcessor(config, progress_interval=args.progress)

    run_live(processor, args.partition, args.max_stamps, args.timeout)

    processor.finalize()
    processor.report()
    return 0 if processor.combined_offsets else 1


if __name__ == "__main__":
    sys.exit(main())
