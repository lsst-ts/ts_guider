# Running the Guider CSC locally against the GDS emulator

How to run `run_guider_csc` inside the development container, fed by
the DAQ/GDS emulator, and watch the events and telemetry it publishes.

The CSC has no guiding commands: it subscribes to the DAQ stamp stream
while ENABLED and publishes results as stamps arrive.

---

## Workspace layout

Everything lives in one workspace directory on the host, which is
mounted into the container as `~/ts_repos`:

```bash
mkdir workspace && cd workspace
git clone https://github.com/lsst-ts/ts_guider.git
git clone https://github.com/lsst-ts/ts_salobj.git
git clone https://github.com/lsst-ts/ts_xml.git
```

- `ts_guider`: the CSC under test.
- `ts_xml`: defines the Guider topics. The container image ships a
  copy, but it may lag `develop`; the checkout is placed first on
  `PYTHONPATH` so the CSC uses the current interface.
- `ts_salobj`: its `docker-compose.yaml` starts Kafka on the host, and
  the checkout is also placed first on `PYTHONPATH` for the same reason
  as `ts_xml`.
- The DAQ SDK (`R5-V13.16`) and the emulator FITS files
  (`guider_fits_emulator/`) also go here, see Prerequisites.

Keep the three checkouts on `develop` (or the ticket branches you are
testing) and pull them before a session.

`ts_xml` and `ts_salobj` are not installed in the container. They are
pure Python and read their data files relative to the package, so
prepending `~/ts_repos/ts_xml/python` and `~/ts_repos/ts_salobj/python`
to `PYTHONPATH` (done in the Environment section below) is all that is
needed to override the copies shipped with the image. Do not
`pip install` them; that would only fight the image's own
installation. The only visible difference from an installed copy is
that `__version__` reads `"?"`, since `version.py` is generated at
install time. `ts_guider` is the one package that must be installed,
because of its `run_guider_csc` entry point, see Prerequisites.

The Kafka broker and schema registry hold no interface definition of
their own. Topics and Avro schemas are registered by the salobj
process that first uses them (the CSC, or `create_topics`), from the
`lsst.ts.xml` that process imports. So the broker ends up with the
interface from the `ts_xml` on `PYTHONPATH`, which is why the clean
broker start below matters: it removes schemas registered from an
older `ts_xml`.

## Starting Kafka

`ts_salobj` ships a `docker-compose.yaml` that starts the `broker`,
`schema-registry` and `kafdrop` containers on a docker network named
`kafka`. Any current `ts_salobj` checkout will do. From the host,
in the workspace root:

```bash
COMPOSE=ts_salobj/docker-compose.yaml

docker compose -f $COMPOSE rm --stop --force
sleep 5
docker compose -f $COMPOSE up -d
```

The `rm --stop --force` first gives a clean broker (no stale topics or
schemas from a previous session). Check with `docker ps` that `broker`
and `schema-registry` are up before starting the development
container.

This step knows nothing about `ts_xml`: the compose file only starts
stock Kafka, schema registry and Kafdrop images. The Guider topics and
schemas are created later, by the CSC (or `create_topics`) running in
the development container, from the `ts_xml` on its `PYTHONPATH`. So
"latest `ts_xml`" is ensured by pulling the checkout and by the
`PYTHONPATH` in the Environment section, not here.

## Starting the container

From the host, with `WORKSPACE` set to the directory that contains
your `ts_guider`, `ts_xml`, `ts_salobj`, ... checkouts and the DAQ SDK:

```bash
WORKSPACE=/path/to/your/workspace

docker run --platform linux/amd64 -it --rm \
    --network kafka \
    --cap-add=NET_ADMIN \
    --name develop_env_guider \
    -v ${WORKSPACE}:/home/saluser/ts_repos \
    lsstts/develop-env:develop
```

- `--network kafka` puts the container on the same network as the
  broker, so `broker:29092` and `schema-registry:8081` resolve.
- `--cap-add=NET_ADMIN` is required for the `ip link` commands in the
  one-time setup that create the `lsst-daq` interface.
- `--platform linux/amd64` is only needed on Apple silicon; the DAQ
  SDK binaries are x86-64.
- `-v` mounts the workspace as `~/ts_repos` inside the container; the
  rest of this document uses that path.
- The stock image has no `sudo`, so run the one-time setup as root
  (`docker exec -u root -it develop_env_guider bash`), or use a
  derived image with `sudo` installed.
- Add `-p 8888:8888` if you want Jupyter from the container.

The container name is your choice; `docker exec` commands below use
`develop_env_guider`.

## Prerequisites

### DAQ SDK

The DAQ SDK is unpacked in the workspace root (e.g. `R5-V13.16`), so
it is visible as `~/ts_repos/R5-V13.16` inside the container. It
ships `dsid_standalone`, `gds_emulator`, etc. prebuilt in `x86/bin`.
Download:
`https://repo-nexus.lsst.org/nexus/repository/daq/daq-sdk/R5-V13.16.tgz`

### guiderGDS module

`guiderGDS` is the C++ bridge to the DAQ stamp stream, which the CSC
imports outside simulation mode. It is built from `cpp/cmake` against
the DAQ SDK. The build directory is not in the repository, so this is
needed on a fresh clone and after changing anything under
`cpp/cmake/src`. `cmake`, `pybind11` and GoogleTest are already in the
container's conda environment:

```bash
cd ~/ts_repos/ts_guider/cpp/cmake
export CCSDAQ=~/ts_repos/R5-V13.16
cmake -S . -B build_v16 -DBUILD_TESTS=ON
cmake --build build_v16 -j
ctest --test-dir build_v16
```

This produces `build_v16/guiderGDS.cpython-*.so`, which is why
`cpp/cmake/build_v16` is on `PYTHONPATH` in the Environment section.
The build output is in the workspace, so unlike the `pip install`
below it survives a container recreation. `-DBUILD_TESTS=ON` is
optional; drop it to skip the C++ unit tests.

### Emulator data

Emulator FITS files are in `~/ts_repos/guider_fits_emulator`.

### ts_guider install

`ts_guider` is installed in the container so the `run_guider_csc`
entry point exists:

```bash
cd ~/ts_repos/ts_guider && pip install -e . --no-deps
```

The install lives in the container, not in the workspace, so redo it
whenever the container is recreated (`run_guider_csc: command not
found`, or `test_bin_script` failing with "Could not find bin script",
are the symptoms).

## One-time container setup

These do not survive a container recreation or a Docker Desktop
restart. If `dsid_standalone` fails with `libreadline.so.7: cannot
open shared object file`, the container is fresh: redo this section.

Inside the container, as root or with `sudo` (`lsst-daq` link and
shared-library shims; the shims point at the conda libraries, so
source the stack first):

```bash
source ~/.setup_dev.sh
bash ~/ts_repos/ts_guider/setup_container.sh
```

On the host, raise the UDP receive buffers (each stamp is ~640 KB;
with the kernel defaults the bursts are dropped). On macOS the
settings live in the Docker Desktop VM:

```bash
bash ts_guider/setup_mac_guider_container.sh
```

On a Linux host set them directly:

```bash
sudo sysctl -w net.core.rmem_max=134217728 \
    net.core.rmem_default=134217728 \
    net.core.netdev_max_backlog=5000
```

## Environment (every terminal)

```bash
docker exec -it develop_env_guider bash
source ~/.setup_dev.sh
export PYTHONPATH=~/ts_repos/ts_xml/python:~/ts_repos/ts_salobj/python:\
~/ts_repos/ts_guider/python:~/ts_repos/ts_guider/cpp/cmake/build_v16:\
$PYTHONPATH
export LSST_KAFKA_BROKER_ADDR=broker:29092
export LSST_SCHEMA_REGISTRY_URL=http://schema-registry:8081
export LSST_SITE=test
export LSST_TOPIC_SUBNAME=sal
export DAQ_SDK=~/ts_repos/R5-V13.16
export LD_LIBRARY_PATH=~/readline-shim:$DAQ_SDK/x86/lib:$LD_LIBRARY_PATH
export PATH=$DAQ_SDK/x86/bin:$PATH
```

`LSST_TOPIC_SUBNAME` is part of the Kafka topic names and must be the
same in every terminal.

To confirm the checkouts, not the copies shipped with the image, are
the ones being imported:

```bash
python -c "import lsst.ts.xml, lsst.ts.salobj; \
print(lsst.ts.xml.__file__); print(lsst.ts.salobj.__file__)"
```

Both paths must start with `/home/saluser/ts_repos/`. A path under
`/opt/lsst/tssw/` means the `PYTHONPATH` export above was skipped in
this terminal.

Optional, but makes the first CSC start faster: pre-create the Kafka
topics once.

```bash
create_topics Guider
```



## Run

Four terminals, each with the environment above.

```bash
# 1. DAQ partition service (leave running)
dsid_standalone -p gds-emu

# 2. The CSC, enabled from the command line
run_guider_csc 1 --state enabled \
    --configdir ~/ts_repos/ts_guider/scripts/emulator_config

# 3. Watch the Guider topics (and the CSC log messages)
python ~/ts_repos/ts_guider/scripts/watch_guider_topics.py

# 4. Feed stamps (from ~/ts_repos)
cd ~/ts_repos
gds_emulator gds-emu 600 guider_fits_emulator/MC_O_20260429_000027_*.fits
```

`scripts/emulator_config/_init.yaml` selects the `gds-emu` partition
and a short warm-up (`seed_frames: 10`). Configuration files must list
every property: salobj does not apply the schema defaults.

## What to expect in the watcher

1. `summaryState: STANDBY`, `DISABLED`, `ENABLED`.
2. On the first stamps: `stateMetadata: START`.
3. After the warm-up locks: one `seriesMetadata` with the sensors and
  their ROIs, then per acquisition `offsets`, `summaryResults`,
   `perGuiderResults` and a `stateMetadata: STAMP`.
4. Running `gds_emulator` again with the `000028` files starts a new
  DAQ sequence: `stateMetadata: STOP` for the old visit, `START` for
   the new one, and a fresh `seriesMetadata`.
5. Stopping the CSC (Ctrl-C) ends the visit with a final `STOP`.



## Troubleshooting

- **CSC stays in DISABLED, no output.** The `enable` command failed.
The CSC does not log to the console; the error and its traceback
appear in the watcher as `log[ERROR]` lines (topic `logMessage`).
- `found another instance with origin=...` A Guider CSC with the
same index is already running (e.g. in another terminal).
- `libreadline.so.7: cannot open shared object file` Redo the
one-time container setup.
- **Stamps arrive but nothing locks.** Check the ROI size against
`GuiderTrackerConfig.cutout_size` (50 px): the measurement window
must fit inside the stamp.

