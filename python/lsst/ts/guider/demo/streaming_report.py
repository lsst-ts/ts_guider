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

__all__ = ["report"]

import sys

import numpy as np

from ..pipeline import StreamingGuiderProcessor


def summarize_milliseconds(durations: list[float]) -> str:
    """Summarize measured durations, converting seconds to milliseconds."""
    if not durations:
        return "n=0"
    array_ms = np.array(durations) * 1e3
    return (
        f"n={array_ms.size} mean={array_ms.mean():.3f} "
        f"median={np.median(array_ms):.3f} "
        f"p95={np.percentile(array_ms, 95):.3f} "
        f"max={array_ms.max():.3f} ms"
    )


def report(processor: StreamingGuiderProcessor) -> None:
    """Print the lifetime counts, timings, and combined-offset summary."""
    metrics = processor.metrics
    locked = sorted(processor.references)
    print("\n=== DAQ guider pipeline metrics ===")
    print(
        f"stamps received      : {metrics.total_stamps}\n"
        f"seed stamps (warm-up): {metrics.seed_stamps}\n"
        f"sensors locked       : {len(locked)} "
        f"({', '.join(locked) if locked else 'none'})\n"
        f"measurements         : "
        f"{metrics.valid_measurements} valid / "
        f"{metrics.invalid_measurements} rejected\n"
        f"acquisitions combined: {len(processor.combined_offsets)}\n"
        f"closed seed frames   : {metrics.discarded_seed_measurements}"
    )
    print(
        "\n--- timing ---\n"
        f"reference lock : {summarize_milliseconds(metrics.lock_times)}\n"
        f"per-stamp HSM  : "
        f"{summarize_milliseconds(metrics.measure_times)}\n"
        f"callback total : "
        f"{summarize_milliseconds(metrics.callback_times)}\n"
        f"per-acq combine: "
        f"{summarize_milliseconds(metrics.combine_times)}"
    )
    if metrics.stream_seconds > 0:
        rate = metrics.total_stamps / metrics.stream_seconds
        print(
            f"stream wall time: {metrics.stream_seconds:.3f} s "
            f"({rate:.1f} stamps/s delivered)"
        )

    _report_combined_offset(processor)
    sys.stdout.flush()


def _report_combined_offset(processor: StreamingGuiderProcessor) -> None:
    """Print the combined-offset summary for the run.

    Reports the frame the combine ran in (camera vs amplifier), the
    run-level offset with its uncertainty, how much the sensors
    disagreed, and the most recent per-acquisition offsets so the
    individual combined results are visible next to the aggregates.
    """
    valid = [c for c in processor.combined_offsets if c.n_valid > 0]
    if not valid:
        print(
            "\n--- combined offset ---\n"
            "no acquisition had a valid sensor; nothing combined."
        )
        return

    amplifier_map = processor.combiner.sensor_amplifiers
    camera_frame = bool(amplifier_map)
    dx = np.array([c.combined_dx for c in valid])
    dy = np.array([c.combined_dy for c in valid])
    error_dx = np.array([c.error_dx for c in valid])
    error_dy = np.array([c.error_dy for c in valid])
    scatter_dx = np.array([c.scatter_dx for c in valid])
    scatter_dy = np.array([c.scatter_dy for c in valid])
    n_valid = np.array([c.n_valid for c in valid])

    print(
        "\n--- combined offset (pixels) ---\n"
        f"combine frame        : "
        f"{'camera' if camera_frame else 'amplifier'}\n"
        f"acquisitions combined: {len(valid)}/"
        f"{len(processor.combined_offsets)} with >=1 valid sensor\n"
        f"sensors per acq      : mean {n_valid.mean():.1f}, "
        f"min {n_valid.min()}, max {n_valid.max()}\n"
        f"overall dx           : {np.median(dx):+.3f} "
        f"+/- {np.nanmedian(error_dx):.3f} px "
        f"(rms over acqs {np.std(dx):.3f})\n"
        f"overall dy           : {np.median(dy):+.3f} "
        f"+/- {np.nanmedian(error_dy):.3f} px "
        f"(rms over acqs {np.std(dy):.3f})\n"
        f"sensor disagreement  : median scatter "
        f"dx {np.nanmedian(scatter_dx):.3f}, "
        f"dy {np.nanmedian(scatter_dy):.3f} px"
    )
    if camera_frame:
        amp_str = ", ".join(
            f"{name}:{amplifier_map[name]}" for name in sorted(amplifier_map)
        )
        print(f"per-sensor amplifier : {amp_str}")

    print("\nlast acquisitions (idx: dx +/- err, dy +/- err, sensors):")
    for offset in valid[-5:]:
        print(
            f"  {offset.stamp_index:6d}: "
            f"dx={offset.combined_dx:+.3f} +/- {offset.error_dx:.3f}  "
            f"dy={offset.combined_dy:+.3f} +/- {offset.error_dy:.3f}  "
            f"({offset.n_valid}/{offset.n_total})"
        )
