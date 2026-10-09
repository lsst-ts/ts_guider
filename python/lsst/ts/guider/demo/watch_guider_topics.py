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

"""Print the Guider CSC events and telemetry as they are published.

Companion to a manual end-to-end run of the Guider CSC against the
GDS emulator. Needs the same Kafka environment as the CSC
(LSST_KAFKA_BROKER_ADDR, LSST_SCHEMA_REGISTRY_URL, LSST_SITE and
LSST_TOPIC_SUBNAME) and the ts_xml under test on PYTHONPATH.

Run in the prepared environment after installing ts_guider::

    watch_guider_topics --index 1

Or run the same command as a Python module::

    python -m lsst.ts.guider.demo.watch_guider_topics --index 1

Select the same SAL index as the CSC (1=MainTel, 2=AuxTel).
Offsets are displayed in micrometers; log messages retain the CSC's units.
Stop with Ctrl-C; the Remote and its Domain are closed on exit.
"""

import argparse
import asyncio
import logging
import os

from lsst.ts import salobj
from lsst.ts.xml.enums.Guider import GuiderStatus, SalIndex


async def show_summary_state(data) -> None:
    """Print the CSC summary state.

    Parameters
    ----------
    data : `salobj.type_hints.BaseMsgType`
        A Guider summaryState event.
    """
    print(f"summaryState     : {salobj.State(data.summaryState).name}", flush=True)


async def show_log_message(data) -> None:
    """Report the CSC log messages.

    Displays combined-offset standard errors, scatter, and counts at
    INFO, as well as command failures and their tracebacks.

    Parameters
    ----------
    data : `salobj.type_hints.BaseMsgType`
        A Guider logMessage event.
    """
    print(
        f"log[{logging.getLevelName(data.level)}] : {data.message.strip()}", flush=True
    )
    if data.traceback:
        print(data.traceback.rstrip(), flush=True)


async def show_error_code(data) -> None:
    """Print nonzero CSC errors.

    Parameters
    ----------
    data : `salobj.type_hints.BaseMsgType`
        A Guider errorCode event; zero indicates no error and is omitted.
    """
    if data.errorCode == 0:
        return
    print(f"errorCode        : {data.errorCode} {data.errorReport}", flush=True)


async def show_state_metadata(data) -> None:
    """Print guider status and acquisition identity.

    Parameters
    ----------
    data : `salobj.type_hints.BaseMsgType`
        A Guider stateMetadata event.
    """
    print(
        f"stateMetadata    : {GuiderStatus(data.status).name} "
        f"seqno={data.seqno} stamp={data.stamp} "
        f"obsid='{data.obsid}' sensors='{data.sensors}'",
        flush=True,
    )


async def show_series_metadata(data) -> None:
    """Print the sensor list and region-of-interest geometry in pixels.

    Parameters
    ----------
    data : `salobj.type_hints.BaseMsgType`
        A Guider seriesMetadata event, including its padded ROI arrays.
    """
    print(
        f"seriesMetadata   : sensors='{data.sensor}' "
        f"roi={data.roiCommonNrows}x{data.roiCommonNcols} "
        f"startrow={list(data.startrow)} "
        f"startcol={list(data.startcol)}",
        flush=True,
    )


async def show_summary_results(data) -> None:
    """Print combined offsets in micrometers and the contributing count.

    Parameters
    ----------
    data : `salobj.type_hints.BaseMsgType`
        A Guider summaryResults event; input offsets are in millimeters.
    """
    print(
        f"summaryResults   : stamp={data.stamp} "
        f"dx={data.deltaX * 1e3:+.2f} um "
        f"dy={data.deltaY * 1e3:+.2f} um "
        f"goodStamps={data.goodStamps}",
        flush=True,
    )


async def show_per_guider_results(data) -> None:
    """Print each named sensor's offsets in micrometers, omitting padding.

    Parameters
    ----------
    data : `salobj.type_hints.BaseMsgType`
        A Guider perGuiderResults event; input offsets are in millimeters.
    """
    sensor_names = data.sensor.split(":") if data.sensor else []
    per_sensor = " ".join(
        f"{name}:({data.centroidDx[i] * 1e3:+.2f},"
        f"{data.centroidDy[i] * 1e3:+.2f})um"
        for i, name in enumerate(sensor_names)
    )
    print(f"perGuiderResults : stamp={data.stamp} {per_sensor}", flush=True)


async def show_offsets(data) -> None:
    """Print offsets telemetry in micrometers.

    Parameters
    ----------
    data : `salobj.type_hints.BaseMsgType`
        A Guider offsets sample; input offsets are in millimeters.
    """
    print(
        f"offsets          : x={data.x * 1e3:+.2f} um y={data.y * 1e3:+.2f} um",
        flush=True,
    )


async def main(index: int = SalIndex.MAIN_TEL) -> None:
    """Watch Guider topics until interrupted or cancelled.

    Parameters
    ----------
    index : `int`, optional
        SAL index: 1 for MainTel (the default), or 2 for AuxTel.

    Notes
    -----
    Uses the Kafka environment and LSST_TOPIC_SUBNAME of the CSC. Installs
    callbacks before reading samples. Closes the read-only Remote and its
    Domain on cancellation or failure, including a failed startup.

    Raises
    ------
    ValueError
        If the index is not a Guider SalIndex value.
    """
    index = SalIndex(index)
    async with salobj.Domain() as domain:
        remote = salobj.Remote(
            domain=domain,
            name="Guider",
            index=index,
            readonly=True,
            include=[
                "summaryState",
                "logMessage",
                "errorCode",
                "stateMetadata",
                "seriesMetadata",
                "summaryResults",
                "perGuiderResults",
                "offsets",
            ],
            start=False,
        )
        try:
            remote.evt_summaryState.callback = show_summary_state
            remote.evt_logMessage.callback = show_log_message
            remote.evt_errorCode.callback = show_error_code
            remote.evt_stateMetadata.callback = show_state_metadata
            remote.evt_seriesMetadata.callback = show_series_metadata
            remote.evt_summaryResults.callback = show_summary_results
            remote.evt_perGuiderResults.callback = show_per_guider_results
            remote.tel_offsets.callback = show_offsets
            await remote.start()
            print(
                f"Watching Guider index={int(index)} ({index.name}) "
                f"LSST_TOPIC_SUBNAME={os.environ['LSST_TOPIC_SUBNAME']!r}; "
                "stop with Ctrl-C.",
                flush=True,
            )
            await asyncio.Event().wait()
        finally:
            await remote.close()


def watch_guider_topics(args: list[str] | None = None) -> None:
    """Run the Guider topic watcher from the command line.

    Parameters
    ----------
    args : `list` [`str`] or `None`, optional
        Command-line arguments. If `None`, read from ``sys.argv``.
        The ``--index`` option selects MainTel (1) or AuxTel (2).

    Notes
    -----
    Requesting help does not open a Kafka connection. Normal operation
    watches until Ctrl-C, closing the Remote and Domain before returning.
    The installed command and Python module invocation use this function.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index",
        type=int,
        choices=[int(index) for index in SalIndex],
        default=int(SalIndex.MAIN_TEL),
        help="SAL index: 1=MainTel, 2=AuxTel (default: 1)",
    )
    options = parser.parse_args(args)
    try:
        asyncio.run(main(index=options.index))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    watch_guider_topics()
