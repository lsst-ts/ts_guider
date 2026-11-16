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
import math
import sys
import threading
from collections.abc import Callable
from time import perf_counter

from ..pipeline import (
    CombinedOffset,
    GuiderTrackerConfig,
    StreamingGuiderProcessor,
)
from .streaming_report import log_visit_summary, report

log = logging.getLogger(__name__)


def make_combined_offset_logger(
    processor: StreamingGuiderProcessor,
) -> Callable[[CombinedOffset], None]:
    """Build an INFO callback for each live combined offset.

    Parameters
    ----------
    processor : `StreamingGuiderProcessor`
        Processor whose latest combination duration belongs to the result
        being reported. The callback does not measure processing time.

    Returns
    -------
    callback : callable
        Logs the acquisition index, camera offsets, sensor scatter,
        standard errors, sensor counts, and combination time in milliseconds.
        The combination time excludes this callback and is not the time
        from DAQ acquisition to delivery of the result.
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
    """Identify the DAQ series before its live acquisition log entries.

    Parameters
    ----------
    sequence : `int` or `None`
        DAQ sequence reported by the processor.
    label : `str`
        Observation or series label from the first stamp, possibly empty.
    """
    log.info("visit start: sequence %s image '%s'", sequence, label or "?")


def run_live(
    processor: StreamingGuiderProcessor,
    partition: str,
    max_stamps: int,
    timeout_seconds: float,
) -> None:
    """Deliver live DAQ stamps to the processor and stop the source.

    Parameters
    ----------
    processor : `StreamingGuiderProcessor`
        Processor to receive each stamp on the DAQ worker thread.
    partition : `str`
        Existing DAQ partition to subscribe to. This function does not
        create or stop the partition service or an emulator.
    max_stamps : `int`
        Nonnegative total stamp-count limit. Zero disables the limit.
    timeout_seconds : `float`
        Finite, nonnegative stream duration limit in seconds. Zero disables
        the deadline. Ctrl-C while streaming also stops the source.

    Notes
    -----
    The CLI validates the limits before calling this function. A normal
    return means the source has stopped and its worker has joined, so the
    caller can finalize the processor. Processing or shutdown errors are
    raised instead; the caller must not finalize a failed run. If stopping
    also fails, preserve the processing error and attach the shutdown error.
    """
    try:
        import guiderGDS
    except ImportError as error:
        raise RuntimeError(
            "Cannot import guiderGDS. Build the C++ Python bindings in "
            "cpp/cmake with the DAQ SDK configured through CCSDAQ, add the "
            "build directory to PYTHONPATH, and make the SDK shared libraries "
            "available on LD_LIBRARY_PATH. See the DAQ stamp source demo "
            "guide in doc/daq_stamp_source_demo.rst."
        ) from error

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

    deadline = None if timeout_seconds == 0 else perf_counter() + timeout_seconds
    run_error: BaseException | None = None
    try:
        source.start_stamp_stream(on_stamp)
        while not done.is_set():
            wait_seconds = 0.5
            if deadline is not None:
                remaining = deadline - perf_counter()
                if remaining <= 0:
                    log.warning(
                        "Timeout: received %d stamps.",
                        processor.metrics.total_stamps,
                    )
                    break
                wait_seconds = min(wait_seconds, remaining)
            done.wait(wait_seconds)
    except KeyboardInterrupt:
        log.info(
            "Interrupted; stopping after %d stamps.",
            processor.metrics.total_stamps,
        )
    except BaseException as error:
        run_error = error
    finally:
        done.set()
        try:
            source.stop_stamp_stream()
        except BaseException as stop_error:
            primary_error = errors[0] if errors else run_error
            if primary_error is not None:
                primary_error.add_note(
                    f"Stopping the DAQ source also failed: {stop_error!r}"
                )
                raise primary_error from stop_error
            raise
    if errors:
        raise errors[0]
    if run_error is not None:
        raise run_error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the guider pipeline over a live DAQ stream for validation."
    )
    parser.add_argument(
        "--partition", default="gds-emu", help="DAQ partition (default: gds-emu)"
    )
    parser.add_argument(
        "--max-stamps",
        type=int,
        default=2000,
        help="Nonnegative stamp limit; 0 disables the limit (default: 2000).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=100.0,
        help="Finite, nonnegative duration in seconds; 0 disables the "
        "deadline (default: 100).",
    )
    parser.add_argument(
        "--seed-frames",
        type=int,
        default=GuiderTrackerConfig.seed_frames,
        help="Positive number of images per sensor before the first reference "
        "selection attempt. Failed attempts retry with each new image "
        "(default: %(default)s).",
    )
    parser.add_argument(
        "--min-snr",
        type=float,
        default=GuiderTrackerConfig.min_snr,
        help="Finite, nonnegative minimum signal-to-noise ratio "
        "(default: %(default)s).",
    )
    parser.add_argument(
        "--progress",
        type=int,
        default=0,
        metavar="N",
        help="Log progress every N stamps; N must be nonnegative, "
        "and 0 disables progress logging.",
    )
    parser.add_argument(
        "--per-frame",
        action="store_true",
        help="Log the combined offset for every acquisition (frame) "
        "live, as the stream runs, not just the final run summary.",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
        help="Logging level for stamps, progress and reports (default: INFO).",
    )
    return parser


def run_daq_guider_pipeline_demo(argv: list[str] | None = None) -> int:
    """Run the tracking pipeline over the live DAQ/GDS stamp stream.

    Uses ``SensorTracker`` and ``OffsetCombiner`` through the shared
    ``StreamingGuiderProcessor``, fed one stamp at a time from the C++
    worker thread. Report measurement, callback, and combination timings
    and optionally log each live result with ``--per-frame``. Reports use
    INFO; DEBUG also includes individual sensor measurements.

    Requires the partition service and GDS emulator (or live DAQ), plus
    the ``guiderGDS`` extension. Importing this module and requesting
    ``--help`` do not open a stream or require the extension.

    Parameters
    ----------
    argv : `list` [`str`] or `None`, optional
        Command-line arguments. If omitted, read ``sys.argv``.

    Returns
    -------
    status : `int`
        Zero if at least one acquisition was combined, otherwise one.
        A combined acquisition may have no accepted sensor measurements.

    Notes
    -----
    Finalize and report only after streaming and shutdown succeed. A
    count limit, timeout, or Ctrl-C while streaming is a normal stop.
    Unexpected failures propagate without further processing or reporting,
    so cleanup does not replace the original error.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.max_stamps < 0:
        parser.error("--max-stamps must be nonnegative")
    if not math.isfinite(args.timeout) or args.timeout < 0:
        parser.error("--timeout must be finite and nonnegative")
    if args.seed_frames <= 0:
        parser.error("--seed-frames must be positive")
    if not math.isfinite(args.min_snr) or args.min_snr < 0:
        parser.error("--min-snr must be finite and nonnegative")
    if args.progress < 0:
        parser.error("--progress must be nonnegative")
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("lsst.ts.guider").setLevel(args.log_level)
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
    run_live(processor, args.partition, args.max_stamps, args.timeout)
    processor.finalize()
    report(processor)
    return 0 if processor.combined_offsets else 1


if __name__ == "__main__":
    sys.exit(run_daq_guider_pipeline_demo())
