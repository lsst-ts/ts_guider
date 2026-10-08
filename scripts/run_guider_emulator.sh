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

# Build or run one component of the local Guider emulator workflow.
# See doc/running_guider_csc.rst. Run as saluser inside the LSST container.

set -eo pipefail

guider_command=${1:-help}
if [[ $# -gt 0 ]]; then shift; fi
case "$guider_command" in
    help|--help|-h)
        cat <<'HELP'
Usage: bash scripts/run_guider_emulator.sh COMMAND [FITS_FILES...]

  setup      Prepare a new container's DAQ interface and library aliases.
  build      Compile guiderGDS, install the Python package, and check imports.
  test       Run CSC tests using Kafka, without a DAQ subscription.
  partition  Start the configured DAQ partition service.
  watch      Display the CSC's topics and diagnostic logs.
  csc        Start the CSC enabled, receiving DAQ stamps.
  emulator   Replay the supplied FITS files twice (one file per sensor).

Run partition, watch, csc, and emulator in separate container terminals.
Each command reads ~/guider_emulator.env; set GUIDER_EMULATOR_SETTINGS
for a different file. Start with examples/guider_emulator.env.
Ctrl-C stops the component in that terminal.
HELP
        exit 0
        ;;
    setup|build|test|partition|watch|csc)
        if [[ $# -ne 0 ]]; then
            echo "$guider_command does not accept additional arguments." >&2
            exit 2
        fi
        ;;
    emulator)
        if [[ $# -lt 1 || $# -gt 8 ]]; then
            echo "Supply one FITS file per sensor, for one to eight sensors." >&2
            exit 2
        fi
        for guider_file in "$@"; do
            if [[ ! -r "$guider_file" ]]; then
                echo "Cannot read FITS file: $guider_file" >&2
                exit 2
            fi
        done
        ;;
    *)
        echo "Unknown command: $guider_command. Use --help for commands." >&2
        exit 2
        ;;
esac

GUIDER_REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
GUIDER_WORKSPACE=$(cd -- "$GUIDER_REPO/.." && pwd -P)
export GUIDER_REPO GUIDER_WORKSPACE
guider_settings=${GUIDER_EMULATOR_SETTINGS:-$HOME/guider_emulator.env}
if [[ ! -r "$guider_settings" ]]; then
    echo "Copy examples/guider_emulator.env to $guider_settings and edit its settings." >&2
    exit 2
fi

# Stack activation may refer to unset shell variables; enable nounset after it.
source /opt/lsst/software/stack/loadLSST.bash
setup lsst_distrib
source "$guider_settings"
set -u
: "${CCSDAQ:?Set CCSDAQ in the settings file.}"
: "${GUIDER_XML:?Set GUIDER_XML in the settings file.}"
: "${GUIDER_SALOBJ:?Set GUIDER_SALOBJ in the settings file.}"
: "${GUIDER_INDEX:?Set GUIDER_INDEX in the settings file.}"
: "${GUIDER_CSC_CONFIG:?Set GUIDER_CSC_CONFIG in the settings file.}"
: "${LSST_KAFKA_BROKER_ADDR:?Set LSST_KAFKA_BROKER_ADDR in the settings file.}"
: "${LSST_SCHEMA_REGISTRY_URL:?Set LSST_SCHEMA_REGISTRY_URL in the settings file.}"
: "${LSST_SITE:?Set LSST_SITE in the settings file.}"
: "${LSST_TOPIC_SUBNAME:?Set LSST_TOPIC_SUBNAME in the settings file.}"
for guider_python_dir in "$GUIDER_XML/python" "$GUIDER_SALOBJ/python"; do
    if [[ ! -d "$guider_python_dir" ]]; then
        echo "Configured Python source directory is missing: $guider_python_dir" >&2
        exit 2
    fi
done
export CCSDAQ LSST_KAFKA_BROKER_ADDR LSST_SCHEMA_REGISTRY_URL LSST_SITE LSST_TOPIC_SUBNAME
export PATH="$CCSDAQ/x86/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/readline-shim:$CCSDAQ/x86/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$GUIDER_XML/python:$GUIDER_SALOBJ/python:$GUIDER_REPO/python:$GUIDER_REPO/cpp/cmake/build_v16:${PYTHONPATH:-}"

case "$guider_command" in
    setup)
        exec bash "$GUIDER_REPO/scripts/setup_guider_emulator_container.sh"
        ;;
    build)
        if ! "$CCSDAQ/x86/bin/gds_emulator" -h >/dev/null; then
            echo "The SDK runtime is not ready. Run the setup command in a new container." >&2
            exit 1
        fi
        # Refresh cached compiler/Python paths after container replacement.
        cmake --fresh -S "$GUIDER_REPO/cpp/cmake" -B "$GUIDER_REPO/cpp/cmake/build_v16" \
            -DBUILD_MODULE=ON -DBUILD_TESTS=OFF \
            -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF
        cmake --build "$GUIDER_REPO/cpp/cmake/build_v16" -j2
        cd "$GUIDER_REPO"
        # Copies of host worktrees may contain an unusable .git pointer.
        if [[ $(git rev-parse --show-toplevel 2>/dev/null || true) != "$GUIDER_REPO" ]]; then
            export SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION:-0.0.0+local}
            echo "Git metadata unavailable; installing development version $SETUPTOOLS_SCM_PRETEND_VERSION."
        fi
        python -m pip install --no-deps --no-build-isolation -e .
        python -c 'from lsst.ts import guider, xml, salobj; import guiderGDS; print("Guider:", guider.__file__); print("XML:", xml.__file__); print("salobj:", salobj.__file__); print("Binding:", guiderGDS.__file__)'
        ;;
    test)
        cd "$GUIDER_REPO"
        exec env -u CCSDAQ -u GUIDER_RUN_EMULATOR python -m pytest -q tests/test_csc.py
        ;;
    watch)
        exec watch_guider_topics --index "$GUIDER_INDEX"
        ;;
    csc)
        exec run_guider_csc "$GUIDER_INDEX" --state enabled --configdir "$GUIDER_CSC_CONFIG"
        ;;
    partition|emulator)
        # Use the same partition as the CSC's base YAML configuration.
        guider_partition=$(python -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["partition"])' "$GUIDER_CSC_CONFIG/_init.yaml")
        if [[ "$guider_command" == partition ]]; then
            guider_args=(dsid_standalone -p "$guider_partition")
        else
            guider_args=(gds_emulator -t 2000 -q 1 -l 2 "$guider_partition" 600 "$@")
        fi
        printf 'DAQ partition: %s\n' "$guider_partition"
        printf 'Running:'
        printf ' %q' "${guider_args[@]}"
        printf '\n'
        exec "${guider_args[@]}"
        ;;
esac
