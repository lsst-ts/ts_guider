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
import dataclasses
import json
import logging
import os
import pathlib
import shutil
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest
from astropy.io import fits
from lsst.ts import guider, salobj
from lsst.ts.xml.enums.Guider import GuiderStatus, SalIndex

STD_TIMEOUT = 30


async def stop_process(process):
    """Reap one owned process, escalating to kill if termination times out."""
    error = None
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        except Exception as caught:
            error = caught
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
    except Exception as caught:
        if error is None:
            error = caught
        else:
            error.add_note(f"Reaping the process also failed: {caught!r}")
    if error is not None:
        raise error


@asynccontextmanager
async def running_process(command, log_path):
    """Retain output and the original error if process cleanup also fails."""
    with log_path.open("w") as output:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=output, stderr=asyncio.subprocess.STDOUT
        )
        error = None
        try:
            yield process
        except BaseException as caught:
            error = caught
            raise
        finally:
            try:
                await stop_process(process)
            except BaseException as cleanup_error:
                if error is None:
                    raise
                error.add_note(f"Stopping {command[0]} also failed: {cleanup_error!r}")


def write_star_files(directory):
    """Write two lossless FITS sequences with known camera-frame motion."""
    paths = []
    camera_offsets = [(0, 0)] * 5 + [(2, -1), (0.4, -0.65), (-1.2, 2.3)]
    rows, columns = np.indices((100, 140))
    for sensor, sensor_index in [("R00_SG0", 3), ("R04_SG0", 39)]:
        raft, slot = sensor.split("_")
        primary = fits.PrimaryHDU()
        primary.header.update(
            {
                "ROIROWS": 100,
                "ROICOLS": 140,
                "ROIMILLI": 200,
                "RAFTBAY": raft,
                "CCDSLOT": slot,
                "ROISEG": "Segment05",
                "ROIROW": sensor_index + 10,
                "ROICOL": sensor_index + 20,
                "GDSSEQ": 1,
                "ROICCDTY": 0,
                "ROIUNDRC": 0,
                "ROIOVERC": 0,
                "ROIOVERR": 0,
                "ROIFLUSH": 2,
                "ROISPLIT": False,
                "FIRMWARE": "2139210d",
                "CONTNUM": "10000",
                "PLATFORM": "lsstcam",
                "OBSID": "guider-csc-test",
                "N_STAMPS": 8,
            }
        )
        hdus = [primary]
        for index, (dx, dy) in enumerate(camera_offsets):
            lx, ly = (-dy, -dx) if sensor_index == 3 else (dx, -dy)
            star = (
                100_000
                / (8 * np.pi)
                * np.exp(-((columns - 70.25 - lx) ** 2 + (rows - 45.7 - ly) ** 2) / 8)
            )
            pixels = np.rint(
                100 + star + np.random.default_rng(index).normal(0, 5, rows.shape)
            ).astype(np.int32)
            hdu = fits.CompImageHDU(pixels, compression_type="RICE_1")
            hdu.header["STMPTMJD"] = 60000 + index / 432000
            hdus.append(hdu)
        template = directory / f"{sensor}.fits"
        fits.HDUList(hdus).writeto(template)
        paths.append(template)
    return paths, camera_offsets


@unittest.skipUnless(
    os.environ.get("GUIDER_RUN_EMULATOR") == "1",
    "Set GUIDER_RUN_EMULATOR=1 to run the CSC/DAQ/Kafka acceptance test.",
)
class GuiderCscEmulatorTestCase(
    salobj.BaseCscTestCase, unittest.IsolatedAsyncioTestCase
):
    """Use a prepared container dedicated to this test's DAQ traffic.

    Requires CCSDAQ, the compiled guiderGDS module, SDK runtime libraries,
    Kafka, and a multicast DAQ interface (lsst-daq by default; override with
    GUIDER_EMULATOR_INTERFACE). Missing prerequisites fail an opted-in run.
    FITS inputs, process logs, CSC logs, and received samples are retained
    in the temporary directory printed by the test, including on failure.
    """

    _randomize_topic_subname = True

    def basic_make_csc(self, initial_state, config_dir, simulation_mode):
        csc = guider.GuiderCsc(
            initial_state=initial_state,
            config_dir=config_dir,
            simulation_mode=simulation_mode,
            index=SalIndex.MAIN_TEL,
        )
        process_stamp = csc.process_stamp

        def record_stamp(pixels, metadata):
            record = {
                name: getattr(metadata, name)
                for name in (
                    "sensor_index",
                    "stamp_index",
                    "sequence",
                    "timestamp_ns",
                    "segment",
                    "startrow",
                    "startcol",
                    "obs_id",
                    "series_id",
                )
            }
            record["thread"] = threading.get_ident()
            self.stamps.append(record)
            process_stamp(pixels, metadata)

        # Forward the genuine native callback unchanged after recording its
        # metadata. The real CSC still runs the processor and publisher.
        csc.process_stamp = record_stamp
        return csc

    async def receive(self, topic):
        """Read and retain a sample delivered through the Remote."""
        sample = await topic.next(flush=False, timeout=STD_TIMEOUT)
        self.samples.setdefault(topic.attr_name, []).append(dataclasses.asdict(sample))
        return sample

    async def next_diagnostic(self):
        """Receive a combined-result log within one overall deadline."""
        async with asyncio.timeout(STD_TIMEOUT):
            while True:
                sample = await self.receive(self.remote.evt_logMessage)
                assert sample.level < logging.ERROR, sample.message
                if sample.message.startswith("Guider combined result:"):
                    assert sample.level == logging.INFO
                    return sample

    async def test_emulator_to_kafka_across_series(self):
        """Send 32 sensor stamps through the SDK, CSC, and Kafka Remote."""
        directory = pathlib.Path(tempfile.mkdtemp(prefix="guider-csc-emulator-"))
        print(f"CSC emulator artifacts: {directory}", flush=True)
        self.stamps = []
        self.samples = {}
        self.run_info = {"topic_subname": os.environ["LSST_TOPIC_SUBNAME"]}
        error = None
        handler = logging.FileHandler(directory / "csc.log")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger = logging.getLogger("Guider")
        logger.addHandler(handler)
        try:
            await self.run_emulator_test(directory)
        except BaseException as caught:
            csc_error = getattr(getattr(self, "csc", None), "_run_error", None)
            error = csc_error if csc_error is not None else caught
            if error is not caught:
                error.add_note(f"The acceptance test also failed: {caught!r}")
                for note in getattr(caught, "__notes__", ()):
                    error.add_note(note)
        finally:
            logger.removeHandler(handler)
            handler.close()
            try:
                (directory / "results.json").write_text(
                    json.dumps(
                        dict(
                            self.run_info,
                            stamps=self.stamps,
                            samples=self.samples,
                            error=repr(error) if error is not None else None,
                        ),
                        indent=2,
                        default=lambda value: value.tolist(),
                    )
                    + "\n"
                )
            except Exception as artifact_error:
                if error is None:
                    error = artifact_error
                else:
                    error.add_note(
                        f"Saving received samples also failed: {artifact_error!r}"
                    )
        if error is not None:
            error.add_note(f"CSC emulator artifacts retained in {directory}")
            raise error

    async def run_emulator_test(self, directory):
        """Own the partition and emulator until the CSC and Remote close."""
        import guiderGDS

        sdk = pathlib.Path(os.environ["CCSDAQ"])
        partition = directory.name
        interface = os.environ.get("GUIDER_EMULATOR_INTERFACE", "lsst-daq")
        self.run_info.update(
            binding=guiderGDS.__file__, partition=partition, interface=interface
        )
        paths, camera_offsets = write_star_files(directory)
        config_dir = directory / "config"
        config_dir.mkdir()
        (config_dir / "_init.yaml").write_text(
            f"partition: {partition}\nseed_frames: 5\nmin_snr: 5.0\n"
        )
        command = [
            str(sdk / "x86/bin/dsid_standalone"),
            "-i",
            interface,
            "-p",
            partition,
            "-s",
            str(directory / "site"),
        ]
        if shutil.which("stdbuf"):
            command = ["stdbuf", "-oL", "-eL", *command]
        loop_thread = threading.get_ident()
        async with running_process(command, directory / "partition.log") as service:
            # Allow the standalone service to register its partition.
            await asyncio.sleep(1)
            assert service.returncode is None, (directory / "partition.log").read_text()
            async with self.make_csc(
                initial_state=salobj.State.ENABLED,
                config_dir=config_dir,
                simulation_mode=0,
            ):
                processor = self.csc.processor
                command = [
                    str(sdk / "x86/bin/gds_emulator"),
                    "-t",
                    "2000",
                    "-q",
                    "65535",
                    "-l",
                    "2",
                    "-n",
                    "8",
                    partition,
                    "200",
                    *map(str, paths),
                ]
                async with running_process(
                    command, directory / "emulator.log"
                ) as emulator:
                    returncode = await asyncio.wait_for(emulator.wait(), STD_TIMEOUT)
                    assert returncode == 0, (directory / "emulator.log").read_text()
                    # Process exit does not establish that every stamp has
                    # reached the callback. Wait for receipt before disabling.
                    async with asyncio.timeout(STD_TIMEOUT):
                        while processor.metrics.total_stamps < 32:
                            if self.csc._run_error is not None:
                                raise self.csc._run_error
                            await asyncio.sleep(0.01)
                    await salobj.set_summary_state(self.remote, salobj.State.DISABLED)
                    assert processor.metrics.total_stamps == len(self.stamps) == 32
                    assert self.csc.processor is self.csc.stamp_source is None
                    assert self.csc._publish_task is self.csc._publish_queue is None
                    assert self.csc._source_stop_future is None
                    assert self.csc._run_error is None
                    threads = {record["thread"] for record in self.stamps}
                    assert len(threads) == 1 and loop_thread not in threads
                    self.run_info["shutdown_drained"] = True
                    await self.check_received_results(camera_offsets)
        assert emulator.returncode == 0
        assert service.returncode is not None
        self.run_info["owned_processes_stopped"] = True

    async def check_received_results(self, camera_offsets):
        """Check received samples and diagnostics after the queue drains."""
        identities = {}
        for record in self.stamps:
            identities.setdefault((record["sequence"], record["stamp_index"]), record)
        assert set(identities) == {
            (sequence, stamp) for sequence in (65535, 0) for stamp in range(1, 9)
        }
        for sequence in (65535, 0):
            for sensor in (3, 39):
                assert [
                    r["stamp_index"]
                    for r in self.stamps
                    if r["sequence"] == sequence and r["sensor_index"] == sensor
                ] == list(range(1, 9))
            series = await self.receive(self.remote.evt_seriesMetadata)
            assert series.sensor == "R00_SG0:R04_SG0"
            assert (series.roiCommonNrows, series.roiCommonNcols) == (100, 140)
            assert list(series.segment[:2]) == [5, 5]
            assert list(series.startrow[:2]) == [13, 49]
            assert list(series.startcol[:2]) == [23, 59]
            for stamp in (6, 7):
                summary = await self.receive(self.remote.evt_summaryResults)
                per_sensor = await self.receive(self.remote.evt_perGuiderResults)
                offsets = await self.receive(self.remote.tel_offsets)
                diagnostic = await self.next_diagnostic()
                identity = identities[(sequence, stamp)]
                timestamp = identity["timestamp_ns"] * 1e-9
                for sample in (summary, per_sensor):
                    assert (sample.seqno, sample.stamp) == (sequence, stamp)
                    assert sample.obsid == identity["series_id"] == "guider-csc-test"
                    assert sample.timestamp == timestamp > 0
                assert summary.goodStamps == 2
                assert per_sensor.sensor == series.sensor
                expected = np.array(camera_offsets[stamp - 1]) * 0.010
                np.testing.assert_allclose(
                    [summary.deltaX, summary.deltaY], expected, atol=0.0005
                )
                np.testing.assert_allclose(
                    [offsets.x, offsets.y], [summary.deltaX, summary.deltaY]
                )
                np.testing.assert_allclose(
                    per_sensor.centroidDx[:2], expected[0], atol=0.0005
                )
                np.testing.assert_allclose(
                    per_sensor.centroidDy[:2], expected[1], atol=0.0005
                )
                assert list(per_sensor.centroidFitQuality[:2]) == [1.0, 1.0]
                assert np.isfinite(per_sensor.momentXx[:2]).all()
                message = diagnostic.message
                assert f"seqno={sequence} stamp={stamp} " in message
                assert "obsid='guider-csc-test'" in message
                assert f"timestamp={timestamp:.9f}" in message
                assert "valid_sensors=2 total_sensors=2" in message
                for field, value in (
                    ("x_mm", summary.deltaX),
                    ("y_mm", summary.deltaY),
                ):
                    actual = float(message.split(field + "=")[1].split()[0])
                    np.testing.assert_allclose(actual, value, atol=1e-9)
                for field in (
                    "x_standard_error_mm",
                    "y_standard_error_mm",
                    "x_scatter_mm",
                    "y_scatter_mm",
                ):
                    assert np.isfinite(float(message.split(field + "=")[1].split()[0]))
        for sequence in (65535, 0):
            for status, stamp in (
                (GuiderStatus.START, 1),
                (GuiderStatus.STAMP, 6),
                (GuiderStatus.STAMP, 7),
                (GuiderStatus.STOP, 8),
            ):
                state = await self.receive(self.remote.evt_stateMetadata)
                identity = identities[(sequence, stamp)]
                assert (state.seqno, state.status, state.stamp) == (
                    sequence,
                    status,
                    stamp,
                )
                assert state.obsid == "guider-csc-test"
                assert state.timestamp == identity["timestamp_ns"] * 1e-9
                if status == GuiderStatus.START:
                    assert state.sensors in ("R00_SG0", "R04_SG0")
                else:
                    assert state.sensors == "R00_SG0:R04_SG0"
        remaining = await asyncio.gather(
            self.remote.evt_stateMetadata.next(flush=False, timeout=1),
            self.remote.evt_summaryResults.next(flush=False, timeout=1),
            self.remote.evt_perGuiderResults.next(flush=False, timeout=1),
            self.remote.tel_offsets.next(flush=False, timeout=1),
            return_exceptions=True,
        )
        assert all(isinstance(result, TimeoutError) for result in remaining), remaining


async def test_exited_process_is_reaped_without_signaling():
    process = Mock(returncode=0, wait=AsyncMock(return_value=0))
    await stop_process(process)
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    process.wait.assert_awaited_once()


async def test_stalled_process_is_killed_and_reaped():
    process = Mock(returncode=None, wait=AsyncMock(side_effect=[TimeoutError(), -9]))
    await stop_process(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.await_count == 2


@pytest.mark.parametrize("run_fails", [False, True])
async def test_cleanup_failure_preserves_original_error(tmp_path, run_fails):
    process = Mock(returncode=None, wait=AsyncMock(return_value=-15))
    process.terminate.side_effect = RuntimeError("termination failed")
    original = ValueError("sample mismatch")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
        with pytest.raises(ValueError if run_fails else RuntimeError) as caught:
            async with running_process(["test-emulator"], tmp_path / "emulator.log"):
                if run_fails:
                    raise original
    if run_fails:
        assert caught.value is original
        assert "termination failed" in " ".join(original.__notes__)
    else:
        assert "termination failed" in str(caught.value)
    process.wait.assert_awaited_once()
