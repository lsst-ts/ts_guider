.. _running-guider-csc:

Running the Guider CSC with the DAQ emulator
==================================================

Use the helper below to build the Guider CSC, feed it guider FITS images,
and watch its results. It sets up the environment for each command from
one settings file. The processes run in separate terminals; Ctrl-C stops
one process at a time.

1. Start the development environment
----------------------------------------

On the host, choose a workspace containing the Guider checkout,
``ts_xml``, ``ts_salobj``, and the unpacked R5-V13.16 SDK. Put the guider
FITS data in a folder of your choice. See :ref:`daq-stamp-source-demo`
for obtaining the SDK and preparing the FITS files.

Build the local image with passwordless ``sudo`` for ``saluser``. Run this
on the host the first time, and again when updating the base image::

    GUIDER_WORKSPACE=/path/to/your/workspace
    cd "$GUIDER_WORKSPACE"
    bash ts_guider/scripts/build_guider_develop_image.sh

The script saves any existing image under a dated tag, pulls the base
image, and builds ``lsstts/develop-env-sudo:develop`` using
``scripts/Dockerfile.develop-env-sudo``. Substitute your checkout's name
for ``ts_guider`` if needed. Once ``lsstts/develop-env:develop`` provides
passwordless ``sudo`` for ``saluser``, skip this build and use that image
in the command below.

Start Kafka and a new development container from the same workspace::

    docker compose -f ts_salobj/docker-compose.yaml up -d broker schema-registry
    docker run --platform linux/amd64 -it --rm \
        --network kafka --cap-add=NET_ADMIN \
        --name develop_env_guider \
        -v "$GUIDER_WORKSPACE:/home/saluser/ts_repos" \
        lsstts/develop-env-sudo:develop

Skip services and containers that are already running. An existing
container must share Kafka's network, provide ``sudo`` for ``saluser``,
and have ``NET_ADMIN`` for DAQ setup.

On the host, prepare UDP receive buffers once per host or Docker VM restart.
Run the script for your platform::

    # macOS with Docker Desktop:
    bash ts_guider/scripts/tune_mac_guider_udp.sh

    # Linux:
    bash ts_guider/scripts/tune_linux_guider_udp.sh

2. Configure and build once
----------------------------------------

Inside the container, as ``saluser``, enter your Guider checkout. Substitute
its directory name if it is not ``ts_guider``::

    cd ~/ts_repos/ts_guider
    cp examples/guider_emulator.env ~/guider_emulator.env

Edit ``~/guider_emulator.env`` to check the SDK, XML, and salobj paths and
choose a topic subname for your run. All terminals read this same file.
The defaults use SAL index 1 and the example ``gds-emu`` CSC configuration.

For a **new container**, prepare the DAQ interface and library aliases
before compiling. This command requires ``sudo``::

    bash scripts/run_guider_emulator.sh setup

This runs ``scripts/setup_guider_emulator_container.sh``. Skip it if you
have already prepared this container. Then build and install::

    bash scripts/run_guider_emulator.sh build

The helper activates the LSST stack, compiles ``guiderGDS`` into
``cpp/cmake/build_v16``, installs the Python package, and checks imports.
Repeat ``build`` after changing C++ sources or recreating the container.
A copied worktree with unavailable Git metadata is installed with the
local development version ``0.0.0+local``.

For the individual build commands and additional tests, see
:ref:`guider-installation`.

3. Start the four components
----------------------------------------

Open each terminal from the host, then enter the checkout::

    docker exec -it develop_env_guider bash
    cd ~/ts_repos/ts_guider

Run one command per terminal, in this order:

**Terminal 1 — partition service.** Skip this if the configured partition
already has a service::

    bash scripts/run_guider_emulator.sh partition

**Terminal 2 — watcher.** Wait for ``Watching Guider``::

    bash scripts/run_guider_emulator.sh watch

**Terminal 3 — CSC.** Wait until the watcher reports
``Subscribed to guider stamps on partition 'gds-emu'.``::

    bash scripts/run_guider_emulator.sh csc

**Terminal 4 — emulator.** Supply one FITS file per sensor, from the same
observation. For example::

    FITS_DIR=/home/saluser/ts_repos/guider_fits_emulator
    bash scripts/run_guider_emulator.sh emulator \
        "$FITS_DIR/MC_O_20260429_000027_R00_SG0_guider.fits" \
        "$FITS_DIR/MC_O_20260429_000027_R04_SG0_guider.fits"

The emulator sends images every 600 milliseconds per sensor and replays
the files twice. The CSC uses normal DAQ operation, without ``--simulate``.
Salobj creates missing Kafka topics and registers their schemas at startup;
no separate topic-creation command is needed for this local setup.

The watcher should show START, then measurements once the tracker selects
a reference star. Each sensor needs at least ten images for its first
reference attempt. A new emulator pass starts a new series and selects
new references. See :ref:`guider-csc` for the result fields and quality cuts.

4. Stop the run
----------------------------------------

When the emulator finishes, the CSC keeps waiting for images. Leave the
watcher running and press Ctrl-C in the CSC terminal. During a healthy
shutdown it finishes the series and attempts to publish STOP. Then stop
the watcher and your partition service in their own terminals.

Check the logs for shutdown errors. If the CSC cannot confirm that its
DAQ worker stopped, restart the CSC process before guiding again.

Checks and troubleshooting
----------------------------------------

List the helper commands without starting any services::

    bash scripts/run_guider_emulator.sh --help

Run the CSC image-processing and lifecycle tests with Kafka::

    bash scripts/run_guider_emulator.sh test

These tests supply simulated images directly to the CSC, without a DAQ
subscription.

To check the complete emulator-to-Kafka path, prepare a separate development
container using steps 1 and 2, without starting the four manual components.
Then run::

    bash scripts/run_guider_emulator.sh test-emulator

This test creates its own FITS images, partition service, CSC, and Remote.
It checks two series and four live results, then stops the processes it
started. It uses a random Kafka topic subname and removes its topics and
schemas afterward. The printed artifact directory retains input images,
logs, and received samples, including on failure. Missing prerequisites
fail the test. Without ``GUIDER_RUN_EMULATOR=1``, the acceptance test is
skipped; the helper sets this flag for you.

The :ref:`installation guide <guider-installation>` lists the processing,
binding, and standalone emulator tests.

* **Missing SDK libraries:** run ``setup`` in a new container before ``build``.
* **Missing sudo:** build the image in step 1 and create a container from it.
  Updating an image does not change an existing container.
* **No watcher output:** check the shared settings file and CSC errors;
  start the emulator only after the subscription message appears.
* **No guiding measurements:** use enough images with a detectable star
  and an ROI large enough for the 50-pixel tracking cutout.
* **Duplicate CSC:** choose a different ``LSST_TOPIC_SUBNAME`` in the
  settings file, then restart the participating processes.
