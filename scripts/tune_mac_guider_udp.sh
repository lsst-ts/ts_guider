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

# Tune UDP reception in the local Docker Desktop Linux VM on macOS.
# Run from the repository root on the Mac, with Docker Desktop active
# and its local context selected: bash scripts/tune_mac_guider_udp.sh
#
# The privileged helper changes VM-wide kernel settings. Reapply after the
# VM restarts. For per-container setup, use
# scripts/setup_guider_emulator_container.sh.
# This script does not start the guider or an emulator.

set -euo pipefail
guider_platform="linux/amd64"

docker run --platform "$guider_platform" --rm --privileged --pid=host alpine \
    nsenter -t 1 -m -u -i -n -p -- \
    sh -c 'sysctl -w net.core.rmem_max=134217728 \
                    net.core.rmem_default=134217728 \
                    net.core.netdev_max_backlog=5000'
