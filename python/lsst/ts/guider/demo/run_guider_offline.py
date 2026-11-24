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

__all__ = ["run_guider_offline"]

import argparse
import logging
import math
from pathlib import Path

from ..guider_orientation import GEOMETRY_SOURCES
from ..pipeline import GuiderTrackerConfig, run_offline

log = logging.getLogger(__name__)


def run_guider_offline(args: list[str] | None = None) -> int:
    """Combine aligned guider FITS sequences and print camera offsets.

    Parameters
    ----------
    args : `list` [`str`] or `None`, optional
        Command-line arguments. If `None`, read from ``sys.argv``.
        Supply one FITS path per sensor. Options select the initial seed
        count, minimum signal-to-noise ratio, geometry source, and log level.

    Returns
    -------
    status : `int`
        Zero if results were produced; one for input/processing errors or
        no combined results. Argument parsing exits with status two for
        invalid arguments, or zero for ``--help``.

    Notes
    -----
    Results go to stdout and logs to stderr. Each result lists the
    acquisition index, camera X/Y offsets, standard errors, sample scatter,
    and valid/total measurement counts. All offsets and statistics are in
    pixels. Geometry defaults to the bundled guider data.
    """
    defaults = GuiderTrackerConfig()
    parser = argparse.ArgumentParser(
        prog="run_guider_offline",
        description="Combine aligned guider FITS sequences into camera-frame offsets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "fits_paths",
        metavar="FITS",
        nargs="+",
        type=Path,
        help="One FITS sequence per sensor, already aligned by acquisition index",
    )
    parser.add_argument(
        "--geometry-source",
        choices=GEOMETRY_SOURCES,
        default="local",
        help="Use bundled guider geometry (local), the LSST camera (lsst), "
        "or the LSST camera when installed (auto)",
    )
    parser.add_argument(
        "--seed-frames",
        type=int,
        default=defaults.seed_frames,
        help="Minimum stamps before the first reference-locking attempt",
    )
    parser.add_argument(
        "--min-snr",
        type=float,
        default=defaults.min_snr,
        help="Minimum aperture signal-to-noise ratio for an accepted measurement",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
        help="Logging level",
    )
    options = parser.parse_args(args)
    if options.seed_frames <= 0:
        parser.error("--seed-frames must be positive")
    if not math.isfinite(options.min_snr) or options.min_snr <= 0:
        parser.error("--min-snr must be finite and positive")

    logging.basicConfig(
        level=options.log_level, format="%(levelname)s %(name)s: %(message)s"
    )
    log.setLevel(options.log_level)
    config = GuiderTrackerConfig(
        seed_frames=options.seed_frames, min_snr=options.min_snr
    )
    log.info(
        "Processing %d FITS files with seed_frames=%d and min_snr=%g",
        len(options.fits_paths),
        config.seed_frames,
        config.min_snr,
    )
    try:
        results = run_offline(
            options.fits_paths, config, geometry_source=options.geometry_source
        )
    except (OSError, ValueError, KeyError, ImportError) as error:
        log.error("Offline processing failed: %s", error)
        return 1
    if not results:
        log.error("No combined offsets were produced.")
        return 1

    print(
        f"{'stamp':>7} {'dx_px':>12} {'dy_px':>12} "
        f"{'error_x_px':>12} {'error_y_px':>12} "
        f"{'scatter_x_px':>12} {'scatter_y_px':>12} "
        f"{'n_valid':>7} {'n_total':>7}"
    )
    for result in results:
        print(
            f"{result.stamp_index:7d} "
            f"{result.combined_dx:12.6f} {result.combined_dy:12.6f} "
            f"{result.error_dx:12.6f} {result.error_dy:12.6f} "
            f"{result.scatter_dx:12.6f} {result.scatter_dy:12.6f} "
            f"{result.n_valid:7d} {result.n_total:7d}"
        )
    log.info("Produced %d combined acquisitions", len(results))
    return 0
