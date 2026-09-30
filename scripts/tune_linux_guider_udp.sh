#!/usr/bin/env bash
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

# Tune UDP reception on a Linux host running the DAQ emulator or receiver.
# Run from the repository root on the host, outside the container:
# bash scripts/tune_linux_guider_udp.sh
#
# Applies the same settings as tune_mac_guider_udp.sh directly to the host.
# Requires sudo and sysctl. These settings last until the host reboots.
# For per-container setup, use scripts/setup_guider_emulator_container.sh.

set -euo pipefail

sudo sysctl -w net.core.rmem_max=134217728 \
    net.core.rmem_default=134217728 \
    net.core.netdev_max_backlog=5000
