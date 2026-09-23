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

__all__ = ["run_daq_stamp_source_demo"]

import argparse
import logging
import threading

import numpy as np

log = logging.getLogger(__name__)


def run_daq_stamp_source_demo(args: list[str] | None = None) -> None:
    """Receive DAQ stamps and log their pixel and sensor metadata.

    Parameters
    ----------
    args : `list` [`str`] or `None`, optional
        Command-line arguments. If `None`, read from ``sys.argv``.
        Options select the partition, stamp count, timeout, and log level.

    Notes
    -----
    Requires the ``guiderGDS`` extension and a running DAQ partition when
    receiving stamps. Importing the module and requesting ``--help`` do
    not open a stream or require the extension. The stream is stopped
    after the requested count, a timeout, or an interruption while waiting.
    """
    parser = argparse.ArgumentParser(
        description="Receive DAQ stamps and log sensor metadata."
    )
    parser.add_argument(
        "--partition", default="gds-emu", help="DAQ partition (default: gds-emu)"
    )
    parser.add_argument(
        "--max-stamps",
        type=int,
        default=400,
        help="Stamp count to receive (default: 400)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=100.0,
        help="Wait timeout in seconds (default: 100)",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
        help="Logging level (default: INFO)",
    )
    options = parser.parse_args(args)
    if options.max_stamps <= 0:
        parser.error("--max-stamps must be positive")
    if not np.isfinite(options.timeout) or options.timeout <= 0:
        parser.error("--timeout must be finite and positive")

    logging.basicConfig(
        level=options.log_level, format="%(levelname)s %(name)s: %(message)s"
    )
    log.setLevel(options.log_level)

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
        partition=options.partition,
        locations=guiderGDS.LocationSet.any(),
    )
    received = []
    done = threading.Event()

    def on_stamp(pixels: np.ndarray, metadata: guiderGDS.StampMetadata) -> None:
        """Report each stamp and signal when the requested count arrives."""
        log.info(
            f"pixels.shape: {pixels.shape}, "
            f"pixels.max: {int(pixels.max())}, "
            f"sensor_index: {metadata.sensor_index}, "
            f"sensor_name: {metadata.sensor_name}, "
            f"segment: {metadata.segment}, "
            f"metadata: {metadata}"
        )
        received.append(metadata)
        if len(received) >= options.max_stamps:
            done.set()

    source.start_stamp_stream(on_stamp)
    try:
        if not done.wait(timeout=options.timeout):
            log.warning("timeout — only received %d stamps", len(received))
    finally:
        source.stop_stamp_stream()
        log.info("received %d stamps in Python", len(received))
