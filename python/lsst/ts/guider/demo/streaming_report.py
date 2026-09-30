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

__all__ = ["log_visit_summary", "report", "summarize_milliseconds"]

import logging

import numpy as np

from ..pipeline import CombinedOffset, StreamingGuiderProcessor

log = logging.getLogger(__name__)


def summarize_milliseconds(durations: list[float]) -> str:
    """Summarize measured durations in milliseconds.

    Parameters
    ----------
    durations : `list` [`float`]
        Individual processing durations in seconds.

    Returns
    -------
    summary : `str`
        Sample count, mean, median, 95th percentile and maximum, or
        ``"n=0"`` if there are no samples.
    """
    if not durations:
        return "n=0"
    array_ms = np.array(durations) * 1e3
    return (
        f"n={array_ms.size} mean={array_ms.mean():.3f} "
        f"median={np.median(array_ms):.3f} "
        f"p95={np.percentile(array_ms, 95):.3f} "
        f"max={array_ms.max():.3f} ms"
    )


def _median_finite(values: list[float]) -> float:
    """Return the median of finite values, or NaN if none are available."""
    array = np.asarray(values)
    finite = array[np.isfinite(array)]
    return float(np.median(finite)) if finite.size else float("nan")


def log_visit_summary(sequence: int | None, offsets: list[CombinedOffset]) -> None:
    """Log the completed DAQ series at INFO level.

    Parameters
    ----------
    sequence : `int` or `None`
        DAQ sequence reported by the processor.
    offsets : `list` [`CombinedOffset`]
        Combined offsets for this series, including results with no
        accepted sensor measurements.

    Notes
    -----
    Offset medians describe the series over time. The separate median
    standard errors summarize individual acquisitions; they are not
    uncertainties on the offset medians. NaN means no finite standard
    error was available, as when every acquisition used one sensor.
    """
    valid = [offset for offset in offsets if offset.n_valid > 0]
    if not valid:
        log.info("visit (sequence %s) complete: no valid offsets", sequence)
        return
    log.info(
        "visit (sequence %s) complete\n"
        "acquisitions                  : %d/%d with >=1 valid sensor\n"
        "median offsets dx, dy         : %+.3f, %+.3f px\n"
        "median acquisition std errors : %.3f, %.3f px",
        sequence,
        len(valid),
        len(offsets),
        np.median([offset.combined_dx for offset in valid]),
        np.median([offset.combined_dy for offset in valid]),
        _median_finite([offset.error_dx for offset in valid]),
        _median_finite([offset.error_dy for offset in valid]),
    )


def report(processor: StreamingGuiderProcessor) -> None:
    """Log the run's counts, timings and combined offsets at INFO level.

    Parameters
    ----------
    processor : `StreamingGuiderProcessor`
        Processor after the source has stopped and finalization succeeded.
        Counts and timing samples cover all DAQ series in the run; locked
        sensors describe the final series. Discarded seed measurements
        are already included in the valid/rejected measurement counts.

    Notes
    -----
    Processing timings exclude DAQ delivery and waiting between stamps.
    Measurement and combination can happen inside a stamp callback, so
    their durations overlap with the callback time and should not be
    added to it.

    The offset median describes combined displacement across acquisitions.
    The median acquisition standard error summarizes their individual
    errors, not the uncertainty of that offset median. Only finite errors
    are included. If none are available, report NaN, as when every
    acquisition used one sensor.
    """
    if not log.isEnabledFor(logging.INFO):
        return
    metrics = processor.metrics
    locked = sorted(processor.references)
    log.info(
        "DAQ guider pipeline metrics\n"
        f"stamps received      : {metrics.total_stamps}\n"
        f"seed stamps (warm-up): {metrics.seed_stamps}\n"
        f"sensors locked (last series): {len(locked)} "
        f"({', '.join(locked) if locked else 'none'})\n"
        f"measurements         : "
        f"{metrics.valid_measurements} valid / "
        f"{metrics.invalid_measurements} rejected\n"
        f"acquisitions combined: {len(processor.combined_offsets)}\n"
        f"discarded seed results: {metrics.discarded_seed_measurements}"
    )
    log.info(
        "processing times\n"
        f"reference lock : {summarize_milliseconds(metrics.lock_times)}\n"
        f"measurement    : "
        f"{summarize_milliseconds(metrics.measure_times)}\n"
        f"callback total : "
        f"{summarize_milliseconds(metrics.callback_times)}\n"
        f"per-acq combine: "
        f"{summarize_milliseconds(metrics.combine_times)}"
    )
    if metrics.stream_seconds > 0:
        rate = metrics.total_stamps / metrics.stream_seconds
        log.info(
            f"stream wall time: {metrics.stream_seconds:.3f} s "
            f"({rate:.1f} stamps/s delivered)"
        )

    _report_combined_offset(processor)


def _report_combined_offset(processor: StreamingGuiderProcessor) -> None:
    """Log offset medians separately from per-acquisition standard errors."""
    valid = [c for c in processor.combined_offsets if c.n_valid > 0]
    if not valid:
        log.info("no acquisition had a valid sensor; no valid combined offsets.")
        return

    amplifier_map = processor.combiner.sensor_amplifiers
    camera_frame = amplifier_map is not None
    dx = np.array([c.combined_dx for c in valid])
    dy = np.array([c.combined_dy for c in valid])
    n_valid = np.array([c.n_valid for c in valid])

    log.info(
        "combined offsets (pixels)\n"
        f"combine frame        : "
        f"{'camera' if camera_frame else 'amplifier'}\n"
        f"acquisitions combined: {len(valid)}/"
        f"{len(processor.combined_offsets)} with >=1 valid sensor\n"
        f"sensors per acq      : mean {n_valid.mean():.1f}, "
        f"min {n_valid.min()}, max {n_valid.max()}\n"
        f"median offsets dx, dy: {np.median(dx):+.3f}, {np.median(dy):+.3f}\n"
        f"temporal std dx, dy  : {np.std(dx):.3f}, {np.std(dy):.3f}\n"
        f"median acquisition std errors: "
        f"{_median_finite([c.error_dx for c in valid]):.3f}, "
        f"{_median_finite([c.error_dy for c in valid]):.3f}\n"
        f"sensor disagreement  : median scatter "
        f"dx {_median_finite([c.scatter_dx for c in valid]):.3f}, "
        f"dy {_median_finite([c.scatter_dy for c in valid]):.3f} px"
    )
    if camera_frame:
        amp_str = ", ".join(
            f"{name}:{amplifier_map[name]}" for name in sorted(amplifier_map)
        )
        log.info("per-sensor amplifier : %s", amp_str or "none")

    log.info("last acquisitions (idx: dx +/- err, dy +/- err, sensors):")
    for offset in valid[-5:]:
        log.info(
            f"  {offset.stamp_index:6d}: "
            f"dx={offset.combined_dx:+.3f} +/- {offset.error_dx:.3f}  "
            f"dy={offset.combined_dy:+.3f} +/- {offset.error_dy:.3f}  "
            f"({offset.n_valid}/{offset.n_total})"
        )
