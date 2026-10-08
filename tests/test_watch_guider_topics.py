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

import asyncio
import importlib
import logging
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from lsst.ts import salobj
from lsst.ts.xml.component_info import ComponentInfo
from lsst.ts.xml.enums.Guider import GuiderStatus

MODULE = "lsst.ts.guider.demo.watch_guider_topics"
watcher = importlib.import_module(MODULE)


@pytest.fixture(params=["console", "module"])
def command(request):
    if request.param == "module":
        return [sys.executable, "-m", MODULE]
    path = shutil.which("watch_guider_topics")
    assert path is not None
    return [path]


@pytest.fixture
def topic_info(monkeypatch):
    """Read the installed Guider XML without opening Kafka clients."""
    monkeypatch.setenv("LSST_SITE", "test")
    return ComponentInfo(name="Guider", topic_subname="test_watcher").topics


async def test_science_topic_units_and_sensor_padding(topic_info, capsys):
    def sample(name, **fields):
        return topic_info[name].make_dataclass()(**fields)

    await watcher.show_series_metadata(
        sample(
            "evt_seriesMetadata",
            sensor="R00_SG0:R00_SG1",
            roiCommonNrows=100,
            roiCommonNcols=160,
            startrow=[10, 30, 0, 0, 0, 0, 0, 0],
            startcol=[20, 40, 0, 0, 0, 0, 0, 0],
        )
    )
    await watcher.show_state_metadata(
        sample(
            "evt_stateMetadata",
            status=GuiderStatus.STAMP,
            seqno=7,
            stamp=15,
            obsid="TEST_OBS_007",
            sensors="R00_SG0:R00_SG1:R04_SG0",
        )
    )
    await watcher.show_offsets(sample("tel_offsets", x=0.03, y=-0.06))
    await watcher.show_summary_results(
        sample(
            "evt_summaryResults",
            stamp=15,
            deltaX=0.03,
            deltaY=-0.06,
            goodStamps=2,
        )
    )
    await watcher.show_per_guider_results(
        sample(
            "evt_perGuiderResults",
            stamp=15,
            sensor="R00_SG0:R00_SG1:R04_SG0",
            centroidDx=[0.02, 0.04, float("nan")] + [123.0] * 5,
            centroidDy=[-0.04, -0.08, float("nan")] + [456.0] * 5,
        )
    )
    output = capsys.readouterr().out
    assert "roi=100x160" in output
    assert "STAMP seqno=7 stamp=15 obsid='TEST_OBS_007'" in output
    assert "dx=+30.00 um dy=-60.00 um goodStamps=2" in output
    assert "x=+30.00 um y=-60.00 um" in output
    assert "R00_SG0:(+20.00,-40.00)um" in output
    assert "R00_SG1:(+40.00,-80.00)um" in output
    assert "R04_SG0:(+nan,+nan)um" in output
    assert "123" not in output and "456" not in output


async def test_info_log_preserves_message_and_units(topic_info, capsys):
    message = "Guider combined result: x_mm=0.03 y_mm=-0.06 x_scatter_mm=nan"
    await watcher.show_log_message(
        topic_info["evt_logMessage"].make_dataclass()(
            level=logging.INFO,
            message=message,
            traceback="",
        )
    )
    assert capsys.readouterr().out == f"log[INFO] : {message}\n"


async def test_empty_sensors_nan_offsets_and_error_traceback(topic_info, capsys):
    await watcher.show_per_guider_results(
        topic_info["evt_perGuiderResults"].make_dataclass()(stamp=1, sensor="")
    )
    await watcher.show_offsets(
        topic_info["tel_offsets"].make_dataclass()(x=float("nan"), y=-0.0015)
    )
    await watcher.show_error_code(
        topic_info["evt_errorCode"].make_dataclass()(errorCode=0)
    )
    await watcher.show_error_code(
        topic_info["evt_errorCode"].make_dataclass()(
            errorCode=3, errorReport="source timed out"
        )
    )
    await watcher.show_log_message(
        topic_info["evt_logMessage"].make_dataclass()(
            level=logging.ERROR,
            message=" processing failed \n",
            traceback="Traceback line\nValueError: bad stamp\n",
        )
    )
    output = capsys.readouterr().out
    assert "perGuiderResults : stamp=1 \n" in output
    assert "x=+nan um y=-1.50 um" in output
    assert output.count("errorCode") == 1
    assert "errorCode        : 3 source timed out" in output
    assert (
        "log[ERROR] : processing failed\nTraceback line\nValueError: bad stamp\n"
        in output
    )


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.setenv("LSST_TOPIC_SUBNAME", "test_watcher_lifecycle")
    events = []
    started = asyncio.Event()
    state = SimpleNamespace(
        failure=None, block_start=False, entered_start=started, remote=None
    )

    class Domain:
        async def __aenter__(self):
            events.append("domain-enter")
            return self

        async def __aexit__(self, *args):
            events.append("domain-close")

    class Remote:
        def __init__(self, **kwargs):
            state.kwargs = kwargs
            if state.failure == "construct":
                raise RuntimeError("construct failed")
            state.remote = self
            self.names = []
            for name in kwargs["include"]:
                attr = ("tel_" if name == "offsets" else "evt_") + name
                self.names.append(attr)
                setattr(self, attr, SimpleNamespace(callback=None))

        async def start(self):
            # A sample delivered immediately on startup must be handled.
            assert all(callable(getattr(self, name).callback) for name in self.names)
            await self.evt_summaryState.callback(
                SimpleNamespace(summaryState=salobj.State.ENABLED)
            )
            events.append("remote-start")
            started.set()
            if state.failure == "start":
                raise RuntimeError("start failed")
            if state.block_start:
                await asyncio.Event().wait()

        async def close(self):
            events.append("remote-close")

    monkeypatch.setattr(watcher.salobj, "Domain", Domain)
    monkeypatch.setattr(watcher.salobj, "Remote", Remote)
    return state, events


@pytest.mark.parametrize("index", [1, 2])
async def test_selected_index_and_cancelled_wait_close_resources(
    lifecycle, capsys, index
):
    state, events = lifecycle
    task = asyncio.create_task(watcher.main(index))
    try:
        await asyncio.wait_for(state.entered_start.wait(), 1)
        assert state.kwargs["index"] == index
        assert state.kwargs["readonly"] is True
        assert state.kwargs["start"] is False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == [
            "domain-enter",
            "remote-start",
            "remote-close",
            "domain-close",
        ]
        output = capsys.readouterr().out
        assert "summaryState     : ENABLED" in output
        assert f"Watching Guider index={index}" in output
        assert "test_watcher_lifecycle" in output
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["construct", "start"])
async def test_startup_failure_closes_owned_resources(lifecycle, failure):
    state, events = lifecycle
    state.failure = failure
    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        await watcher.main()
    assert events[-1] == "domain-close"
    assert events.count("remote-close") == (failure == "start")


async def test_cancelled_start_closes_remote_and_domain(lifecycle):
    state, events = lifecycle
    state.block_start = True
    task = asyncio.create_task(watcher.main())
    try:
        await asyncio.wait_for(state.entered_start.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events[-2:] == ["remote-close", "domain-close"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "args, expected", [(["--help"], 0), (["--index", "0"], 2), (["--index", "3"], 2)]
)
def test_cli_does_not_require_kafka_for_help_or_invalid_index(
    command, args, expected, tmp_path
):
    env = os.environ.copy()
    for key in (
        "LSST_TOPIC_SUBNAME",
        "LSST_KAFKA_BROKER_ADDR",
        "LSST_SCHEMA_REGISTRY_URL",
    ):
        env.pop(key, None)
    result = subprocess.run(
        [*command, *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == expected, result.stderr
    assert "--index" in result.stdout + result.stderr
    assert "LSST_TOPIC_SUBNAME" not in result.stderr
    assert "Traceback" not in result.stderr
