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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

__all__ = ["AlignmentDetailedState", "AlignmentCSC", "run_guider"]

import asyncio
import enum

from lsst.ts import salobj

from . import __version__
from .guider_model import GuiderModel
from .config_schema import CONFIG_SCHEMA



class GuiderDetailedState(enum.IntEnum):
    DISABLED = 1
    ENABLED = 2
    FAULT = 3
    OFFLINE = 4
    STANDBY = 5
    MEASURING = 6
    GUIDING = 7


class GuiderCSC(salobj.ConfigurableCsc):
    """CSC to control the MT Guider system.

    Parameters
    ----------
    config_dir : `str` (optional)
        Directory of configuration files, or None for the standard
        configuration directory (obtained from `get_default_config_dir`).
        This is provided for unit testing.
    initial_state : `salobj.State` (optional)
        The initial state of the CSC. Typically one of:
        - State.ENABLED if you want the CSC immediately usable.
        - State.STANDBY if you want full emulation of a CSC.
    override : `str`, optional
        Configuration override file to apply if ``initial_state`` is
        `State.DISABLED` or `State.ENABLED`.
    simulation_mode : `int`, optional
        Simulation mode; one of:

        * 0: normal operation
        * 1: use the simulation features of SpatialAnalyzer
        * 2: minimal internal simulator with canned responses
    """

    version = __version__
    valid_simulation_modes = (0, 1, 2)
    simulation_help = """
    Simulation modes TBD
    """

    def __init__(
        self,
        config_dir=None,
        initial_state=salobj.State.STANDBY,
        override="",
        simulation_mode=0,
    ):
        super().__init__(
            name="guider",
            index=0,
            config_schema=CONFIG_SCHEMA,
            config_dir=config_dir,
            initial_state=initial_state,
            override=override,
            simulation_mode=simulation_mode,
        )
        self.model = None
        self.max_iters = 3

        # temporary variables for position; these will eventually be supplied
        # by the TMA and camera rotator CSCs.
        self.elevation = 90
        self.azimuth = 0
        self.camrot = 0
        self.last_measurement = None

    async def handle_summary_state(self):
        if self.disabled_or_enabled:
            if self.model is None:
                self.model = GuiderModel(
                    host=self.config.guider_host,
                    log=self.log,
                )
                await self.model.connect()
                self.log.debug(
                    f"connected to GDS at {self.model.host}"
                )
        else:
            if self.model is not None:
                await self.model.disconnect()
                self.model = None

    async def configure(self, config):
        self.config = config
        if self.model is not None:
            if self.model.connected:
                await self.model.disconnect()
            await self.model.connect()

    @staticmethod
    def get_config_pkg():
        return "ts_config_mttcs"

    async def do_measureGuideStars(self, data):
        self.log.debug("measure guide stars")
        """Measure and return guide star centroids."""
        self.assert_enabled()
        await self.model.measure_guidestars(data.guidestars)
        result = await self.model.get_guidestars_position(data.guidestars)
        self.log.debug(
            self.parse_offsets(result)
        )  # TODO publish an event with the measured coords
        self.last_measurement = self.parse_offsets(result)
        await self.evt_guidingStatus.set_write(
            guidestars=self.last_measurement,
        )

    async def do_align(self, data):
        """Perform correction loop"""
        self.assert_enabled()
        await self.model.align()

    async def do_healthCheck(self, data):
        """run healthcheck script"""
        self.assert_enabled()


def run_guider():
    """Run the guider CSC."""
    asyncio.run(GuiderCSC.amain(index=None))

