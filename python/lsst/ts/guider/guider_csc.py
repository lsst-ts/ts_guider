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

__all__ = ["GuiderCsc", "run_guider_csc"]

import asyncio

from lsst.ts import salobj

from . import __version__
from .config_schema import CONFIG_SCHEMA


class GuiderCsc(salobj.ConfigurableCsc):
    """Guider CSC.

    The Guider interface provides the standard CSC commands only.

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


def run_guider_csc() -> None:
    """Run the Guider CSC."""
    asyncio.run(GuiderCsc.amain(index=True))
