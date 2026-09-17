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
import threading

import numpy as np


def run_daq_stamp_source_demo(args: list[str] | None = None) -> None:
    """Receive DAQ stamps and print their pixel and sensor metadata.

    Parameters
    ----------
    args : `list` [`str`] or `None`, optional
        Command-line arguments. If `None`, read from ``sys.argv``.
        Options select the partition, stamp count, and timeout.

    Notes
    -----
    Requires the ``guiderGDS`` extension and a running DAQ partition when
    receiving stamps. Importing the module and requesting ``--help`` do
    not open a stream or require the extension. The stream is stopped
    after the requested count, a timeout, or an interruption while waiting.
    """
    parser = argparse.ArgumentParser(
        description="Receive DAQ stamps and print sensor metadata."
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
    options = parser.parse_args(args)
    if options.max_stamps <= 0:
        parser.error("--max-stamps must be positive")
    if not np.isfinite(options.timeout) or options.timeout <= 0:
        parser.error("--timeout must be finite and positive")

    import guiderGDS

    source = guiderGDS.DaqStampSource(
        partition=options.partition,
        locations=guiderGDS.LocationSet.any(),
    )
    received = []
    done = threading.Event()

    def on_stamp(pixels: np.ndarray, metadata: guiderGDS.StampMetadata) -> None:
        """Report each stamp and signal when the requested count arrives."""
        print(
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
            print(f"timeout — only received {len(received)} stamps")
    finally:
        source.stop_stamp_stream()
        print(f"received {len(received)} stamps in Python")
