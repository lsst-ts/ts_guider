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

__all__ = ["GuiderCsc", "run_guider_csc"]

import asyncio

from lsst.ts import salobj
from lsst.ts.xml.enums.Guider import GuiderStatus

from . import __version__
from .config_schema import CONFIG_SCHEMA

# Number of guider sensors reported by the Guider interface.
# Matches the Count: 8 used in Guider_Commands.xml /
# Guider_Events.xml for roiXLeft, roiXRight, roiYBottom,
# roiYTop and status.
NUMBER_OF_GUIDER_SENSORS = 8


class GuiderCsc(salobj.ConfigurableCsc):
    """Guider CSC.

    Parameters
    ----------
    config_dir : `str`, `pathlib.Path`, or `None`, optional
        Directory of configuration files, or `None` for the
        standard configuration directory (obtained from
        `get_config_pkg`).
    initial_state : `salobj.State` or `int`, optional
        The initial state of the CSC. This is provided
        for unit testing, as real CSCs should start up in
        `lsst.ts.salobj.State.STANDBY`, the default.
    simulation_mode : `int`, optional
        Simulation mode.
    index : `int` or `None`, optional
        SAL index; must be a valid Guider index
        (1=MainTel, 2=AuxTel).

    Raises
    ------
    salobj.ExpectedError
        If ``initial_state`` or ``simulation_mode``
        is invalid.

    Notes
    -----
    **Simulation Modes**

    Supported simulation modes:

    * 0: regular operation
    * 1: simulation mode
    """

    valid_simulation_modes = (0, 1)
    version = __version__

    def __init__(
        self,
        config_dir=None,
        initial_state=salobj.State.STANDBY,
        simulation_mode=0,
        index=None,
    ):
        self.config = None
        self._is_guiding = False
        self._have_started_guiding = False

        super().__init__(
            name="Guider",
            index=index,
            config_schema=CONFIG_SCHEMA,
            config_dir=config_dir,
            initial_state=initial_state,
            simulation_mode=simulation_mode,
        )

    @staticmethod
    def get_config_pkg():
        return "ts_config_ocs"

    async def configure(self, config):
        self.config = config

    async def start(self):
        await super().start()
        await self.evt_guidingStatus.set_write(
            status=[GuiderStatus.STOPPED] * NUMBER_OF_GUIDER_SENSORS,
        )

    async def begin_disable(self, data):
        self._is_guiding = False
        self._have_started_guiding = False
        await self.evt_guidingStatus.set_write(
            status=[GuiderStatus.STOPPED] * NUMBER_OF_GUIDER_SENSORS,
        )

    async def do_startGuiding(self, data):
        """Start guiding using the input information.

        The command returns as soon as guiding starts,
        before the first iteration is completed. If it
        fails to find a guide star in the provided guider
        region, the CSC will go to fault state.

        Parameters
        ----------
        data : ``cmd_startGuiding.DataType``
            Command data with the following fields:

            * roiXLeft (long[8], pixel): origin of the
              guider ROI window in x for each of the 8
              guider sensors.
            * roiXRight (long[8], pixel): end of the
              guider ROI window in x for each sensor.
              roiXLeft == roiXRight == 0 means no guider
              region for that sensor.
            * roiYBottom (long[8], pixel): origin of the
              guider ROI window in y for each sensor.
            * roiYTop (long[8], pixel): end of the guider
              ROI window in y for each sensor. Same
              "both zero means disabled" convention as x.
            * expTime (float, ms): exposure time; common
              to all guider sensors.
            * binning (long, pixel): binning factor;
              common to all guider sensors.
        """
        self.assert_enabled()
        raise NotImplementedError("Start Guiding is not implemented yet.")

    async def do_stopGuiding(self, data):
        """Stop current guider operation.

        Parameters
        ----------
        data : ``cmd_stopGuiding.DataType``
            Command data (no parameters).
        """
        self.assert_enabled()
        raise NotImplementedError("Stop Guiding is not implemented yet.")

    async def do_resumeGuiding(self, data):
        """Resume current guider operation.

        This command will restart a guider operation
        with the last values used in startGuiding. If
        no previous startGuiding command was issued
        since the last time the CSC was enabled, the
        command will be rejected.

        Parameters
        ----------
        data : ``cmd_resumeGuiding.DataType``
            Command data (no parameters).
        """
        self.assert_enabled()
        raise NotImplementedError("Resume Guiding is not implemented yet.")


def run_guider_csc() -> None:
    """Run the Guider CSC."""
    asyncio.run(GuiderCsc.amain(index=True))
