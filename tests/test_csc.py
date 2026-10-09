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
import logging
import pathlib
import sys
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from lsst.ts import guider, salobj
from lsst.ts.guider.error_code import ErrorCode
from lsst.ts.xml.enums.Guider import GuiderStatus, SalIndex

STD_TIMEOUT = 60.0
TEST_CONFIG_DIR = pathlib.Path(__file__).parent / "data" / "config"

# Stamp size must exceed GuiderTrackerConfig.cutout_size (50) with
# margin, so the measurement window never crosses the stamp edge.
STAMP_SIZE = 200
STAR_SIGMA_PIXELS = 2.0
STAR_FLUX = 200_000.0
BACKGROUND_LEVEL = 100.0
NOISE_SIGMA = 10.0

# GDS sensor_index values that decode to guide sensors R00_SG0 and
# R00_SG1 (bay 0, board 1; see sensor_identifiers).
SENSOR_INDEX_R00_SG0 = 3
SENSOR_INDEX_R00_SG1 = 4


@contextmanager
def mock_daq_binding(binding):
    """Replace only the binding, preserving other modules imported by the CSC.

    patch.dict(sys.modules) restores the entire mapping and drops camera
    imports performed during startup; re-importing the pybind classes then
    fails because their Python extensions have already been registered.
    """
    missing = object()
    previous = sys.modules.get("guiderGDS", missing)
    sys.modules["guiderGDS"] = binding
    try:
        yield
    finally:
        if previous is missing:
            del sys.modules["guiderGDS"]
        else:
            sys.modules["guiderGDS"] = previous


@dataclasses.dataclass
class StampMetadata:
    """Stand-in for the guiderGDS StampMetadata binding."""

    sensor_index: int
    stamp_index: int
    sequence: int = 1
    timestamp_ns: int = 0
    segment: int = 0
    startrow: int = 100
    startcol: int = 200
    obs_id: str = "TEST_OBS_001"
    series_id: str = "TEST_SERIES"
    sensor_name: str = ""


def make_star_stamp(
    center_x: float, center_y: float, rng: np.random.Generator
) -> np.ndarray:
    """Make one stamp with a bright Gaussian star plus noise."""
    grid_y, grid_x = np.mgrid[0:STAMP_SIZE, 0:STAMP_SIZE]
    variance = STAR_SIGMA_PIXELS**2
    star = (
        STAR_FLUX
        * np.exp(
            -((grid_x - center_x) ** 2 + (grid_y - center_y) ** 2) / (2.0 * variance)
        )
        / (2.0 * np.pi * variance)
    )
    noise = rng.normal(scale=NOISE_SIGMA, size=star.shape)
    return (star + noise + BACKGROUND_LEVEL).astype(np.float32)


class GuiderCscTestCase(salobj.BaseCscTestCase, unittest.IsolatedAsyncioTestCase):
    # BaseCscTestCase removes only this test's topics and schemas at teardown.
    _randomize_topic_subname = True

    def basic_make_csc(self, initial_state, config_dir, simulation_mode, override=""):
        return guider.GuiderCsc(
            initial_state=initial_state,
            config_dir=config_dir,
            simulation_mode=simulation_mode,
            index=SalIndex.MAIN_TEL,
            override=override,
        )

    async def test_bin_script(self):
        await self.check_bin_script(
            name="Guider",
            index=SalIndex.MAIN_TEL,
            exe_name="run_guider_csc",
            cmdline_args=("--configdir", str(TEST_CONFIG_DIR)),
        )

    async def test_standard_state_transitions(self):
        async with self.make_csc(
            initial_state=salobj.State.STANDBY,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            await self.check_standard_state_transitions(enabled_commands=())

    async def feed_stamps(
        self,
        first_stamp_index: int,
        number_of_stamps: int,
        rng: np.random.Generator,
        sequence: int = 1,
        worker_thread: bool = False,
        blank: bool = False,
        offset: tuple[float, float] = (0.0, 0.0),
        sensor_indices: tuple[int, ...] = (SENSOR_INDEX_R00_SG0, SENSOR_INDEX_R00_SG1),
        **metadata_overrides,
    ) -> None:
        """Feed ordered acquisitions with chosen motion and metadata.

        Offsets are in amplifier pixels relative to each star's seed position.
        Worker calls are awaited in order, as required by ``process_stamp``.
        """
        sensors = (
            (SENSOR_INDEX_R00_SG0, 120.0, 80.0),
            (SENSOR_INDEX_R00_SG1, 90.0, 130.0),
        )
        for stamp_index in range(
            first_stamp_index, first_stamp_index + number_of_stamps
        ):
            for sensor_index, center_x, center_y in sensors:
                if sensor_index not in sensor_indices:
                    continue
                pixels = make_star_stamp(
                    center_x + offset[0], center_y + offset[1], rng
                )
                if blank:
                    pixels.fill(0)
                metadata = StampMetadata(
                    sensor_index=sensor_index,
                    stamp_index=stamp_index % 65536,
                    sequence=sequence,
                    timestamp_ns=1_700_000_000_000_000_000
                    + stamp_index * 1_000_000_000,
                    **metadata_overrides,
                )
                if worker_thread:
                    await asyncio.to_thread(self.csc.process_stamp, pixels, metadata)
                else:
                    self.csc.process_stamp(pixels, metadata)
            # Let the publish loop drain between acquisitions.
            await asyncio.sleep(0)

    async def next_state_metadata_with_status(self, status: GuiderStatus):
        """Read the requested status within one overall deadline."""
        async with asyncio.timeout(STD_TIMEOUT):
            while True:
                data = await self.remote.evt_stateMetadata.next(flush=False)
                if data.status == status:
                    return data

    async def next_combined_log(self):
        """Read a combined diagnostic and reject publication errors."""
        async with asyncio.timeout(STD_TIMEOUT):
            while True:
                data = await self.remote.evt_logMessage.next(flush=False)
                assert data.level < logging.ERROR, data.message
                if data.message.startswith("Guider combined result:"):
                    assert data.level == logging.INFO
                    return data

    async def next_result(self):
        """Receive one live result and its diagnostic from the Remote."""
        state, summary, per_sensor, offsets, diagnostic = await asyncio.gather(
            self.next_state_metadata_with_status(GuiderStatus.STAMP),
            self.remote.evt_summaryResults.next(flush=False, timeout=STD_TIMEOUT),
            self.remote.evt_perGuiderResults.next(flush=False, timeout=STD_TIMEOUT),
            self.remote.tel_offsets.next(flush=False, timeout=STD_TIMEOUT),
            self.next_combined_log(),
        )
        return SimpleNamespace(
            state=state,
            summary=summary,
            per_sensor=per_sensor,
            offsets=offsets,
            diagnostic=diagnostic,
        )

    async def assert_no_result_samples(self):
        """Check that shutdown did not replay seed or trailing corrections."""
        results = await asyncio.gather(
            self.remote.evt_summaryResults.next(flush=False, timeout=1),
            self.remote.evt_perGuiderResults.next(flush=False, timeout=1),
            self.remote.tel_offsets.next(flush=False, timeout=1),
            return_exceptions=True,
        )
        assert all(isinstance(result, TimeoutError) for result in results), results

    async def test_guiding_publishes_events_and_telemetry(self):
        rng = np.random.default_rng(seed=42)
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            processor = self.csc.processor
            queue = self.csc._publish_queue
            # Five stationary seeds establish the reference. Move both stars
            # by (2, -1) amplifier pixels for the live acquisitions.
            await self.feed_stamps(0, 5, rng, worker_thread=True)
            await self.feed_stamps(5, 5, rng, worker_thread=True, offset=(2, -1))

            state_start = await self.next_state_metadata_with_status(GuiderStatus.START)
            assert state_start.seqno == 1
            assert state_start.stamp == 0
            assert state_start.obsid == "TEST_OBS_001"
            assert state_start.sensors == "R00_SG0"

            series = await self.remote.evt_seriesMetadata.next(
                flush=False, timeout=STD_TIMEOUT
            )
            assert series.sensor == "R00_SG0:R00_SG1"
            assert (series.roiCommonNrows, series.roiCommonNcols) == (
                STAMP_SIZE,
                STAMP_SIZE,
            )
            assert list(series.startrow[:2]) == [100, 100]
            assert list(series.startcol[:2]) == [200, 200]

            # C00 flips X on both detectors. R00_SG0 has nQuarter=5 and
            # R00_SG1 has nQuarter=2: their camera offsets are (1, -2) and
            # (2, 1) pixels, respectively. Their mean is (0.015, -0.005) mm.
            for stamp in range(5, 9):
                result = await self.next_result()
                for sample in (result.state, result.summary, result.per_sensor):
                    assert sample.seqno == 1
                    assert sample.stamp == stamp
                    assert sample.obsid == "TEST_OBS_001"
                    assert sample.timestamp == 1_700_000_000.0 + stamp
                assert result.state.sensors == "R00_SG0:R00_SG1"
                assert result.summary.goodStamps == 2
                assert result.per_sensor.sensor == "R00_SG0:R00_SG1"
                assert list(result.per_sensor.centroidFitQuality[:2]) == [1.0, 1.0]
                np.testing.assert_allclose(
                    result.per_sensor.centroidDx[:2], [0.01, 0.02], atol=0.0002
                )
                np.testing.assert_allclose(
                    result.per_sensor.centroidDy[:2], [-0.02, 0.01], atol=0.0002
                )
                assert np.isfinite(result.per_sensor.momentXx[:2]).all()
                np.testing.assert_allclose(
                    (result.summary.deltaX, result.summary.deltaY),
                    (0.015, -0.005),
                    atol=0.0002,
                )
                np.testing.assert_allclose(
                    (result.offsets.x, result.offsets.y),
                    (result.summary.deltaX, result.summary.deltaY),
                )
                message = result.diagnostic.message
                assert f"seqno=1 stamp={stamp} " in message
                assert "obsid='TEST_OBS_001'" in message
                assert f"timestamp={1_700_000_000.0 + stamp:.9f}" in message
                assert "valid_sensors=2 total_sensors=2" in message
                for field, expected in (
                    ("x_mm", result.summary.deltaX),
                    ("y_mm", result.summary.deltaY),
                    ("x_standard_error_mm", 0.005),
                    ("y_standard_error_mm", 0.015),
                    ("x_scatter_mm", np.sqrt(2) * 0.005),
                    ("y_scatter_mm", np.sqrt(2) * 0.015),
                ):
                    value = float(message.split(field + "=")[1].split()[0])
                    np.testing.assert_allclose(value, expected, atol=0.0002)

            await salobj.set_summary_state(self.remote, salobj.State.DISABLED)
            stop = await self.next_state_metadata_with_status(GuiderStatus.STOP)
            assert (stop.seqno, stop.stamp) == (1, 9)
            assert stop.sensors == "R00_SG0:R00_SG1"
            assert self.csc.processor is None
            assert self.csc._publish_task is None
            assert self.csc._publish_queue is None
            await self.assert_no_result_samples()

            await salobj.set_summary_state(self.remote, salobj.State.ENABLED)
            assert self.csc.processor is not processor
            assert self.csc._publish_queue is not queue
            assert self.csc.processor.metrics.total_stamps == 0
            # The new run must use its own reference, even at a new position.
            await self.feed_stamps(
                0, 7, rng, sequence=2, offset=(10, 5), worker_thread=True
            )
            result = await self.next_result()
            assert (result.summary.seqno, result.summary.stamp) == (2, 5)
            np.testing.assert_allclose(
                (result.summary.deltaX, result.summary.deltaY), (0, 0), atol=0.0002
            )

    async def test_delayed_reference_publishes_results_and_logs(self):
        rng = np.random.default_rng(seed=91)
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            await self.feed_stamps(0, 4, rng, blank=True, worker_thread=True)
            await self.feed_stamps(4, 3, rng, worker_thread=True)
            assert self.csc.processor.references == {}
            assert all(
                tracker.pending_seed_count == 7
                for tracker in self.csc.processor.trackers.values()
            )
            # The eighth seed locks, acquisition 8 is measured live,
            # and acquisition 9 closes it for publication.
            await self.feed_stamps(7, 3, rng, worker_thread=True)
            result = await self.next_result()
            series = await self.remote.evt_seriesMetadata.next(
                flush=False, timeout=STD_TIMEOUT
            )
            assert (
                result.state.stamp
                == result.summary.stamp
                == result.per_sensor.stamp
                == 8
            )
            assert (
                result.state.timestamp
                == result.summary.timestamp
                == result.per_sensor.timestamp
                == 1_700_000_008.0
            )
            assert result.summary.goodStamps == 2
            assert series.roiCommonNrows == STAMP_SIZE
            assert "seqno=1 stamp=8 " in result.diagnostic.message
            np.testing.assert_allclose(
                (result.offsets.x, result.offsets.y),
                (result.summary.deltaX, result.summary.deltaY),
            )
            assert np.isfinite((result.offsets.x, result.offsets.y)).all()
            assert all(
                tracker.pending_seed_count == 0
                for tracker in self.csc.processor.trackers.values()
            )

    async def test_new_sequence_starts_new_visit(self):
        rng = np.random.default_rng(seed=17)
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            await self.feed_stamps(
                first_stamp_index=0, number_of_stamps=8, rng=rng, sequence=65535
            )
            await self.next_state_metadata_with_status(GuiderStatus.START)
            # The new sequence ends the first visit and re-acquires.
            await self.feed_stamps(
                first_stamp_index=0, number_of_stamps=8, rng=rng, sequence=0
            )
            state_stop = await self.next_state_metadata_with_status(GuiderStatus.STOP)
            assert state_stop.seqno == 65535
            assert state_stop.stamp == 7
            assert state_stop.timestamp == 1700000007.0
            state_start = await self.next_state_metadata_with_status(GuiderStatus.START)
            assert state_start.seqno == 0
            state_stamp = await self.next_state_metadata_with_status(GuiderStatus.STAMP)
            assert state_stamp.seqno == 0

            # The same ROI must still produce a series event for the new START.
            first = await self.remote.evt_seriesMetadata.next(
                flush=False, timeout=STD_TIMEOUT
            )
            second = await self.remote.evt_seriesMetadata.next(
                flush=False, timeout=STD_TIMEOUT
            )
            assert first.sensor == second.sensor == "R00_SG0:R00_SG1"
            assert list(first.startrow) == list(second.startrow)
            assert list(first.startcol) == list(second.startcol)
            reference = self.csc.processor.references["R00_SG0"]

            await self.feed_stamps(
                0,
                8,
                rng,
                sequence=1,
                segment=5,
                startrow=300,
                startcol=400,
                obs_id="NEW_OBSERVATION",
            )
            third = await self.remote.evt_seriesMetadata.next(
                flush=False, timeout=STD_TIMEOUT
            )
            assert list(third.segment[:2]) == [5, 5]
            assert list(third.startrow[:2]) == [300, 300]
            assert list(third.startcol[:2]) == [400, 400]
            assert self.csc.processor.references["R00_SG0"] is not reference

    async def test_missing_sensor_is_not_reported_as_rejected(self):
        rng = np.random.default_rng(54)
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            await self.feed_stamps(0, 5, rng, worker_thread=True)
            references = self.csc.processor.references
            # Both sensors have references, but only SG0 supplies live images.
            # An image label change within this sequence must not reseed.
            await self.feed_stamps(
                5,
                2,
                rng,
                worker_thread=True,
                offset=(2, -1),
                sensor_indices=(SENSOR_INDEX_R00_SG0,),
                obs_id="NEXT_IMAGE",
            )
            result = await self.next_result()
            assert result.state.sensors == result.per_sensor.sensor == "R00_SG0"
            assert result.summary.goodStamps == 1
            assert (result.summary.seqno, result.summary.stamp) == (1, 5)
            assert result.summary.obsid == "NEXT_IMAGE"
            for name, reference in references.items():
                assert self.csc.processor.references[name] is reference
            np.testing.assert_allclose(
                (result.summary.deltaX, result.summary.deltaY),
                (0.01, -0.02),
                atol=0.0002,
            )
            assert list(result.summary.qualityFlag[:1]) == [1.0]
            assert np.isnan(result.summary.qualityFlag[1:]).all()
            assert np.isnan(result.per_sensor.centroidDx[1:]).all()
            series = await self.remote.evt_seriesMetadata.next(
                flush=False, timeout=STD_TIMEOUT
            )
            assert series.sensor == "R00_SG0:R00_SG1"
            assert "valid_sensors=1 total_sensors=1" in result.diagnostic.message
            assert "x_standard_error_mm=nan" in result.diagnostic.message
            assert "x_scatter_mm=nan" in result.diagnostic.message
            await salobj.set_summary_state(self.remote, salobj.State.DISABLED)
            stop = await self.next_state_metadata_with_status(GuiderStatus.STOP)
            assert stop.sensors == "R00_SG0:R00_SG1"
            assert stop.obsid == "NEXT_IMAGE"
            await self.assert_no_result_samples()

    async def test_subscription_failure_faults_and_can_recover(self):
        async with self.make_csc(
            initial_state=salobj.State.DISABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=0,
        ):
            source = Mock()
            binding = SimpleNamespace(
                DaqStampSource=Mock(side_effect=RuntimeError("no test partition")),
                LocationSet=SimpleNamespace(any=lambda: "all"),
            )
            with mock_daq_binding(binding):
                with salobj.assertRaisesAckError():
                    await self.remote.cmd_enable.start(timeout=STD_TIMEOUT)
                error = await self.next_failure()
                assert self.csc.summary_state == salobj.State.FAULT
                assert error.errorCode == ErrorCode.DAQ_SUBSCRIPTION
                assert "no test partition" in error.errorReport
                assert self.csc.processor is None
                assert self.csc._publish_task is None
                binding.DaqStampSource.side_effect = None
                binding.DaqStampSource.return_value = source
                await salobj.set_summary_state(self.remote, salobj.State.ENABLED)
                binding.DaqStampSource.assert_called_with(
                    partition="test", locations="all"
                )
                source.start_stamp_stream.assert_called_once_with(
                    self.csc.process_stamp
                )
                await salobj.set_summary_state(self.remote, salobj.State.DISABLED)
                source.stop_stamp_stream.assert_called_once()
                assert self.csc._publish_task is None

    async def next_failure(self, timeout=STD_TIMEOUT):
        """Receive a nonzero error and a FAULT summary state."""
        # The initial healthy sample may arrive after a local queue flush.
        async with asyncio.timeout(timeout):
            while True:
                error = await self.remote.evt_errorCode.next(
                    flush=False, timeout=timeout
                )
                if error.errorCode != 0:
                    break
            while True:
                state = await self.remote.evt_summaryState.next(
                    flush=False, timeout=timeout
                )
                if state.summaryState == salobj.State.FAULT:
                    return error

    async def test_worker_processing_failure_faults(self):
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            self.remote.evt_errorCode.flush()
            failure = RuntimeError("bad stamp")
            processor = self.csc.processor
            processor.finalize = Mock()
            self.csc.processor.on_stamp = Mock(side_effect=failure)
            await asyncio.to_thread(
                self.csc.process_stamp,
                np.zeros((100, 100)),
                StampMetadata(sensor_index=3, stamp_index=0),
            )
            error = await self.next_failure()
            assert error.errorCode == ErrorCode.STAMP_PROCESSING
            assert "bad stamp" in error.errorReport
            await asyncio.wait_for(self.csc._processing_fault_task, timeout=STD_TIMEOUT)
            assert self.csc.summary_state == salobj.State.FAULT
            assert self.csc._run_error is failure
            processor.finalize.assert_not_called()
            assert self.csc.processor is None
            assert self.csc._publish_task is None
            async with asyncio.timeout(STD_TIMEOUT):
                while True:
                    log_sample = await self.remote.evt_logMessage.next(flush=False)
                    if "Failed to process a guider stamp" in log_sample.message:
                        break
            assert log_sample.level == logging.ERROR
            assert "RuntimeError: bad stamp" in log_sample.traceback

    async def test_short_series_and_configuration_override(self):
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
            override="short.yaml",
        ):
            assert self.csc.processor.config.seed_frames == 3
            assert self.csc.processor.config.min_snr == 7.0
            await self.feed_stamps(0, 1, np.random.default_rng(17))
            await salobj.set_summary_state(self.remote, salobj.State.DISABLED)
            stop = await self.next_state_metadata_with_status(GuiderStatus.STOP)
            assert stop.seqno == 1
            assert stop.stamp == 0
            assert stop.obsid == "TEST_OBS_001"
            assert self.csc.processor is None
            assert self.csc._publish_queue is None
            await self.assert_no_result_samples()

    async def test_series_without_a_reference_publishes_no_corrections(self):
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            await self.feed_stamps(
                0, 8, np.random.default_rng(12), blank=True, worker_thread=True
            )
            processor = self.csc.processor
            assert not processor.references
            assert all(
                tracker.pending_seed_count == 8
                for tracker in processor.trackers.values()
            )
            await salobj.set_summary_state(self.remote, salobj.State.DISABLED)
            stop = await self.next_state_metadata_with_status(GuiderStatus.STOP)
            assert (stop.seqno, stop.stamp) == (1, 7)
            assert all(
                tracker.pending_seed_count == 0
                for tracker in processor.trackers.values()
            )
            assert self.csc.processor is None
            assert self.csc._publish_task is self.csc._publish_queue is None
            await self.assert_no_result_samples()

    async def test_stamp_rollover_keeps_wire_identity(self):
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            await self.feed_stamps(
                65530, 9, np.random.default_rng(42), worker_thread=True
            )
            results = [
                await self.remote.evt_summaryResults.next(
                    flush=False, timeout=STD_TIMEOUT
                )
                for _ in range(3)
            ]
            assert [result.stamp for result in results] == [65535, 0, 1]
            assert [result.seqno for result in results] == [1, 1, 1]
            assert results[1].timestamp == 1_700_065_536.0
            assert results[1].obsid == "TEST_OBS_001"

    async def test_daq_stop_deadline_faults_while_join_is_pending(self):
        source = Mock()
        entered, release = threading.Event(), threading.Event()

        def stop():
            entered.set()
            if not release.wait(30):
                raise RuntimeError("Test did not release the mock DAQ join")

        source.stop_stamp_stream.side_effect = stop
        binding = SimpleNamespace(
            DaqStampSource=Mock(return_value=source),
            LocationSet=SimpleNamespace(any=lambda: "all"),
        )
        with mock_daq_binding(binding), patch(
            "lsst.ts.guider.guider_csc.SOURCE_STOP_TIMEOUT", 0.2
        ):
            async with self.make_csc(
                initial_state=salobj.State.ENABLED,
                config_dir=TEST_CONFIG_DIR,
                simulation_mode=0,
            ):
                processor = self.csc.processor
                processor.finalize = Mock()
                self.remote.evt_errorCode.flush()
                try:
                    with salobj.assertRaisesAckError():
                        await self.remote.cmd_disable.start(timeout=STD_TIMEOUT)
                    error = await self.next_failure()
                    assert entered.is_set()
                    assert error.errorCode == ErrorCode.SHUTDOWN
                    assert "native join is still pending" in error.errorReport
                    assert self.csc.summary_state == salobj.State.FAULT
                    assert self.csc.stamp_source is source
                    assert self.csc._source_stop_failed
                    assert not self.csc._source_stop_future.done()
                    assert not self.csc._source_stop_future.cancelled()
                    assert self.csc._publish_task is None
                    processor.finalize.assert_not_called()
                finally:
                    release.set()
                    if self.csc._source_stop_future is not None:
                        await asyncio.wait_for(
                            asyncio.shield(self.csc._source_stop_future), 5
                        )
                await salobj.set_summary_state(self.remote, salobj.State.DISABLED)
                with salobj.assertRaisesAckError():
                    await self.remote.cmd_enable.start(timeout=STD_TIMEOUT)
                assert self.csc.summary_state == salobj.State.FAULT
                source.start_stamp_stream.assert_called_once()
                source.stop_stamp_stream.assert_called_once()
                assert self.csc.processor is processor
                assert (
                    "restart the CSC process" in self.csc.evt_errorCode.data.errorReport
                )

    async def test_failed_daq_stop_requires_process_restart(self):
        source = Mock()
        failure = RuntimeError("DAQ join failed")
        source.stop_stamp_stream.side_effect = failure
        binding = SimpleNamespace(
            DaqStampSource=Mock(return_value=source),
            LocationSet=SimpleNamespace(any=lambda: "all"),
        )
        with mock_daq_binding(binding):
            async with self.make_csc(
                initial_state=salobj.State.ENABLED,
                config_dir=TEST_CONFIG_DIR,
                simulation_mode=0,
            ):
                processor = self.csc.processor
                processor.finalize = Mock()
                with salobj.assertRaisesAckError():
                    await self.remote.cmd_disable.start(timeout=STD_TIMEOUT)
                error = await self.next_failure()
                assert self.csc.summary_state == salobj.State.FAULT
                assert error.errorCode == ErrorCode.SHUTDOWN
                assert "DAQ join failed" in error.errorReport
                assert self.csc._run_error is failure
                assert self.csc.stamp_source is source
                assert self.csc._publish_task is None
                processor.finalize.assert_not_called()
                await salobj.set_summary_state(self.remote, salobj.State.DISABLED)
                with salobj.assertRaisesAckError():
                    await self.remote.cmd_enable.start(timeout=STD_TIMEOUT)
                assert self.csc.summary_state == salobj.State.FAULT
                source.start_stamp_stream.assert_called_once()
                source.stop_stamp_stream.assert_called_once()
                assert self.csc.processor is processor
                assert (
                    "restart the CSC process" in self.csc.evt_errorCode.data.errorReport
                )
