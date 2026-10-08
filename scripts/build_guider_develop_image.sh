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

# Build the local development image used by the Guider emulator recipe.
# Run on the Docker host. No image is pushed to a registry.

set -euo pipefail

if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
    cat <<'HELP'
Usage: bash scripts/build_guider_develop_image.sh

Save the previous local image under a dated tag (if present), pull the
base image, and rebuild the sudo-enabled image without the build cache.
The script can be run from any directory.

Optional environment settings:
  GUIDER_BASE_IMAGE  lsstts/develop-env:develop
  GUIDER_IMAGE       lsstts/develop-env-sudo:develop
  GUIDER_PLATFORM    linux/amd64 (required by the current DAQ SDK)
HELP
    exit 0
fi
if [[ $# -ne 0 ]]; then
    echo "Unexpected arguments. Use --help for usage." >&2
    exit 2
fi

guider_base_image=${GUIDER_BASE_IMAGE:-lsstts/develop-env:develop}
guider_image=${GUIDER_IMAGE:-lsstts/develop-env-sudo:develop}
guider_platform=${GUIDER_PLATFORM:-linux/amd64}
guider_scripts_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

if [[ "$guider_image" == "$guider_base_image" ]]; then
    echo "GUIDER_IMAGE must differ from GUIDER_BASE_IMAGE." >&2
    exit 2
fi

# Fail before changing tags if Docker is unavailable.
docker info >/dev/null
if docker image inspect "$guider_image" >/dev/null 2>&1; then
    guider_previous_image="$guider_image-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    docker tag "$guider_image" "$guider_previous_image"
    echo "Previous image saved as $guider_previous_image"
fi

echo "Pulling $guider_base_image for $guider_platform"
docker pull --platform "$guider_platform" "$guider_base_image"
echo "Building $guider_image"
# This Dockerfile needs no checkout files, so send only the Dockerfile.
docker build --platform "$guider_platform" --no-cache \
    --build-arg "BASE_IMAGE=$guider_base_image" \
    --tag "$guider_image" - < "$guider_scripts_dir/Dockerfile.develop-env-sudo"
echo "Built $guider_image"
