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

import asyncio
import dataclasses
import pathlib
import unittest

import numpy as np
from lsst.ts import guider, salobj
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
# R00_SG1 (bay 0, board 1; see sensor_orientation).
SENSOR_INDEX_R00_SG0 = 3
SENSOR_INDEX_R00_SG1 = 4


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
    def basic_make_csc(self, initial_state, config_dir, simulation_mode):
        return guider.GuiderCsc(
            initial_state=initial_state,
            config_dir=config_dir,
            simulation_mode=simulation_mode,
            index=SalIndex.MAIN_TEL,
        )

    async def test_bin_script(self):
        await self.check_bin_script(
            name="Guider",
            index=SalIndex.MAIN_TEL,
            exe_name="run_guider_csc",
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
    ) -> None:
        """Feed stamps for both test sensors, one acquisition at a time."""
        sensors = (
            (SENSOR_INDEX_R00_SG0, 120.0, 80.0),
            (SENSOR_INDEX_R00_SG1, 90.0, 130.0),
        )
        for stamp_index in range(
            first_stamp_index, first_stamp_index + number_of_stamps
        ):
            for sensor_index, center_x, center_y in sensors:
                pixels = make_star_stamp(center_x, center_y, rng)
                metadata = StampMetadata(
                    sensor_index=sensor_index,
                    stamp_index=stamp_index,
                    sequence=sequence,
                    timestamp_ns=1_700_000_000_000_000_000
                    + stamp_index * 1_000_000_000,
                )
                self.csc.process_stamp(pixels, metadata)
            # Let the publish loop drain between acquisitions.
            await asyncio.sleep(0)

    async def next_state_metadata_with_status(self, status: GuiderStatus):
        """Read stateMetadata samples until one with the given status."""
        while True:
            data = await self.remote.evt_stateMetadata.next(
                flush=False, timeout=STD_TIMEOUT
            )
            if data.status == status:
                return data

    async def test_guiding_publishes_events_and_telemetry(self):
        rng = np.random.default_rng(seed=42)
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            # 5 seed frames lock the reference at stamp_index 4; live
            # acquisitions 5..8 are combined as 6..9 arrive.
            await self.feed_stamps(first_stamp_index=0, number_of_stamps=10, rng=rng)

            state_start = await self.next_state_metadata_with_status(GuiderStatus.START)
            assert state_start.seqno == 1
            assert state_start.obsid == "TEST_OBS_001"

            series_metadata = await self.remote.evt_seriesMetadata.next(
                flush=False, timeout=STD_TIMEOUT
            )
            assert series_metadata.sensor == "R00_SG0:R00_SG1"
            assert series_metadata.roi_common_nrows == STAMP_SIZE
            assert series_metadata.roi_common_ncols == STAMP_SIZE
            assert list(series_metadata.startrow[:2]) == [100, 100]
            assert list(series_metadata.startcol[:2]) == [200, 200]

            state_stamp = await self.next_state_metadata_with_status(GuiderStatus.STAMP)
            assert state_stamp.stamp == 5
            assert state_stamp.sensors == "R00_SG0:R00_SG1"

            summary_results = await self.remote.evt_summaryResults.next(
                flush=False, timeout=STD_TIMEOUT
            )
            assert summary_results.seqno == 1
            assert summary_results.stamp == 5
            assert summary_results.obsid == "TEST_OBS_001"
            assert summary_results.good_stamps == 2
            assert abs(summary_results.delta_x) < 0.1
            assert abs(summary_results.delta_y) < 0.1

            per_guider_results = await self.remote.evt_perGuiderResults.next(
                flush=False, timeout=STD_TIMEOUT
            )
            assert per_guider_results.sensor == "R00_SG0:R00_SG1"
            assert per_guider_results.stamp == 5
            assert list(per_guider_results.centroid_fit_quality[:2]) == [
                1.0,
                1.0,
            ]
            assert np.isfinite(per_guider_results.centroid_dx[:2]).all()
            assert np.isfinite(per_guider_results.moment_xx[:2]).all()

            offsets = await self.remote.tel_offsets.next(
                flush=False, timeout=STD_TIMEOUT
            )
            assert offsets.n_sensors == 2
            assert abs(offsets.x) < 0.1
            assert abs(offsets.y) < 0.1

            # Disabling stops guiding and ends the visit.
            await salobj.set_summary_state(self.remote, salobj.State.DISABLED)
            state_stop = await self.next_state_metadata_with_status(GuiderStatus.STOP)
            assert state_stop.sensors == "R00_SG0:R00_SG1"

    async def test_new_sequence_starts_new_visit(self):
        rng = np.random.default_rng(seed=17)
        async with self.make_csc(
            initial_state=salobj.State.ENABLED,
            config_dir=TEST_CONFIG_DIR,
            simulation_mode=1,
        ):
            await self.feed_stamps(
                first_stamp_index=0, number_of_stamps=8, rng=rng, sequence=1
            )
            await self.next_state_metadata_with_status(GuiderStatus.START)
            # The new sequence ends the first visit and re-acquires.
            await self.feed_stamps(
                first_stamp_index=0, number_of_stamps=8, rng=rng, sequence=2
            )
            state_stop = await self.next_state_metadata_with_status(GuiderStatus.STOP)
            assert state_stop.seqno == 1
            state_start = await self.next_state_metadata_with_status(GuiderStatus.START)
            assert state_start.seqno == 2
            state_stamp = await self.next_state_metadata_with_status(GuiderStatus.STAMP)
            assert state_stamp.seqno == 2
