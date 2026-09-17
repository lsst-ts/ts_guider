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

# Sphinx configuration for single-package documentation builds.

import lsst.ts.guider  # noqa
from documenteer.conf.guide import *  # noqa

project = "ts_guider"
html_theme_options["logotext"] = project  # type: ignore # noqa
html_title = project
html_short_title = project

intersphinx_mapping["ts_salobj"] = ("https://ts-salobj.lsst.io", None)  # type: ignore # noqa
intersphinx_mapping["ts_xml"] = ("https://ts-xml.lsst.io", None)  # type: ignore # noqa
