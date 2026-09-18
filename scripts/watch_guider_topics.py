# This file is part of ts_guider.
#
# Developed for Vera C. Rubin Observatory Telescope and Site Systems.
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Print the Guider CSC events and telemetry as they are published.

Companion to a manual end-to-end run of the Guider CSC against the
GDS emulator. Needs the same Kafka environment as the CSC
(LSST_KAFKA_BROKER_ADDR, LSST_SCHEMA_REGISTRY_URL, LSST_SITE and
LSST_TOPIC_SUBNAME) and the ts_xml under test on PYTHONPATH.

Run (inside the container)::

    python watch_guider_topics.py

Stop with Ctrl-C.
"""

import asyncio
import logging

from lsst.ts import salobj
from lsst.ts.xml.enums.Guider import GuiderStatus, SalIndex


async def show_summary_state(data) -> None:
    print(f"summaryState     : {salobj.State(data.summaryState).name}")


async def show_log_message(data) -> None:
    """Report the CSC log messages.

    The CSC writes no console output of its own, so a command that
    fails (e.g. an enable that cannot reach the DAQ) is only visible
    here.
    """
    print(f"log[{logging.getLevelName(data.level)}] : {data.message.strip()}")
    if data.traceback:
        print(data.traceback.rstrip())


async def show_error_code(data) -> None:
    if data.errorCode == 0:
        return
    print(f"errorCode        : {data.errorCode} {data.errorReport}")


async def show_state_metadata(data) -> None:
    print(
        f"stateMetadata    : {GuiderStatus(data.status).name} "
        f"seqno={data.seqno} stamp={data.stamp} "
        f"obsid='{data.obsid}' sensors='{data.sensors}'"
    )


async def show_series_metadata(data) -> None:
    print(
        f"seriesMetadata   : sensors='{data.sensor}' "
        f"roi={data.roi_common_nrows}x{data.roi_common_ncols} "
        f"startrow={list(data.startrow)} "
        f"startcol={list(data.startcol)}"
    )


async def show_summary_results(data) -> None:
    print(
        f"summaryResults   : stamp={data.stamp} "
        f"dx={data.delta_x * 1e3:+.2f} +/- {data.delta_x_err * 1e3:.2f} um "
        f"dy={data.delta_y * 1e3:+.2f} +/- {data.delta_y_err * 1e3:.2f} um "
        f"good_stamps={data.good_stamps}"
    )


async def show_per_guider_results(data) -> None:
    sensor_names = data.sensor.split(":") if data.sensor else []
    per_sensor = " ".join(
        f"{name}:({data.centroid_dx[i] * 1e3:+.2f},"
        f"{data.centroid_dy[i] * 1e3:+.2f})um"
        for i, name in enumerate(sensor_names)
    )
    print(f"perGuiderResults : stamp={data.stamp} {per_sensor}")


async def show_offsets(data) -> None:
    print(
        f"offsets          : x={data.x * 1e3:+.2f} um "
        f"y={data.y * 1e3:+.2f} um n_sensors={data.n_sensors}"
    )


async def main() -> None:
    async with salobj.Domain() as domain:
        remote = salobj.Remote(domain=domain, name="Guider", index=SalIndex.MAIN_TEL)
        await remote.start_task
        remote.evt_summaryState.callback = show_summary_state
        remote.evt_logMessage.callback = show_log_message
        remote.evt_errorCode.callback = show_error_code
        remote.evt_stateMetadata.callback = show_state_metadata
        remote.evt_seriesMetadata.callback = show_series_metadata
        remote.evt_summaryResults.callback = show_summary_results
        remote.evt_perGuiderResults.callback = show_per_guider_results
        remote.tel_offsets.callback = show_offsets
        print("Watching Guider topics; stop with Ctrl-C.")
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
