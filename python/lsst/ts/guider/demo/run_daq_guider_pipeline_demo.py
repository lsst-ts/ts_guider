# This file is part of ts_guider.
#
# Developed for the Vera C. Rubin Observatory Telescope and Site Systems.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

__all__ = ["run_daq_guider_pipeline_demo"]

import argparse
import logging
import sys
import threading
from collections.abc import Callable
from time import perf_counter

import numpy as np

from ..pipeline import (
    CombinedOffset,
    GuiderTrackerConfig,
    StreamingGuiderProcessor,
)
from .streaming_report import report

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
    errors: list[Exception] = []

    def on_stamp(pixels, metadata):
        if done.is_set():
            return
        try:
            processor.on_stamp(pixels, metadata)
        except Exception as error:
            # The pybind callback boundary logs and contains exceptions.
            # Return this one to the main thread after stopping the source.
            errors.append(error)
            done.set()
            return
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
    if errors:
        raise errors[0]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the guider pipeline over the live DAQ stamp stream."
    )
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


def run_daq_guider_pipeline_demo(argv: list[str] | None = None) -> int:
    """Run the tracking pipeline over the live DAQ/GDS stamp stream.

    Uses ``SensorTracker`` and ``OffsetCombiner`` through the shared
    ``StreamingGuiderProcessor``, fed one stamp at a time from the C++
    worker thread. Report measurement, callback, and combination timings
    and optionally print each live result with ``--per-frame``.

    Requires the partition service and GDS emulator (or live DAQ), plus
    the ``guiderGDS`` extension. Importing this module and requesting
    ``--help`` do not open a stream or require the extension.
    """
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

    # Construct the camera before subscribing. Its first lazy build can
    # otherwise block stamp handling and the next series' discovery ACK.
    processor.combiner.orientation.camera
    try:
        run_live(processor, args.partition, args.max_stamps, args.timeout)
    finally:
        processor.finalize()
        report(processor)
    return 0 if processor.combined_offsets else 1


if __name__ == "__main__":
    sys.exit(run_daq_guider_pipeline_demo())
