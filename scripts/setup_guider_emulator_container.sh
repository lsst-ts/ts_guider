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

# Prepare a new Linux development container for local DAQ emulation.
# Create the lsst-daq dummy interface and SDK library compatibility links.
# Use this inside the container on either a Linux or macOS host.
# Run from the repository root inside the container:
# bash scripts/setup_guider_emulator_container.sh
# Requires iproute2, sudo, network administration privileges, and an active
# LSST conda environment. The lsst-daq interface must not already exist.
# Tune host UDP reception separately with tune_linux_guider_udp.sh or
# tune_mac_guider_udp.sh in this directory.
#
# The library aliases reflect the original R5-V13.16 development setup.
# Use them only with SDK and conda libraries verified to be ABI compatible.
# Add $HOME/readline-shim to LD_LIBRARY_PATH in shells running SDK programs.

set -euo pipefail
: "${CONDA_PREFIX:?Activate the LSST conda environment before running this recipe.}"

sudo ip link add lsst-daq type dummy
sudo ip addr add 192.168.100.1/24 dev lsst-daq
sudo ip link set lsst-daq up multicast on
sudo ip link set lsst-daq mtu 9000

mkdir -p "$HOME/readline-shim"
ln -sf "$CONDA_PREFIX/lib/libreadline.so.8" \
    "$HOME/readline-shim/libreadline.so.7"
ln -sf "$CONDA_PREFIX/lib/libcfitsio.so" \
    "$HOME/readline-shim/libcfitsio.so.7"
ln -sf "$CONDA_PREFIX/lib/libbz2.so.1.0" \
    "$HOME/readline-shim/libbz2.so.1.0"
