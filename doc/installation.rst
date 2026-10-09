.. _guider-installation:

Building and testing the guider pipeline
========================================

The Python tracking code runs in the LSST environment. Receiving live
DAQ stamps also requires the DAQ SDK and the ``guiderGDS`` C++ extension.
The Python package and that extension are built separately.

The DAQ commands below use the Linux x86-64 SDK. On macOS, build and run
the DAQ components inside the Linux development container described in
:ref:`daq-stamp-source-demo`. That guide also covers starting a container,
obtaining the SDK, and arranging the emulator FITS files.

Activate the development environment
----------------------------------------

In an existing LSST development container, activate the stack:

.. code-block:: bash

   source /opt/lsst/software/stack/loadLSST.bash
   setup lsst_distrib

For a separately installed Linux stack, use its activation script instead.
The environment must provide the package's Python dependencies: NumPy,
SciPy, Astropy, GalSim, and PyYAML, along with compatible ``ts_salobj``,
``ts_xml``, and ``obs_lsst`` packages. The camera model comes from
``lsst.obs.lsst``. CSC configuration and tests also require ``ts_config_ocs``.
Even a standalone tracking import loads the package's CSC imports, so
``ts_salobj`` and ``ts_xml`` must be available.

Choose the checkout to work on and keep these paths consistent in every
terminal:

.. code-block:: bash

   export GUIDER_REPO=/path/to/ts_guider
   cd "$GUIDER_REPO"
   export PYTHONPATH="$GUIDER_REPO/python:${PYTHONPATH:-}"

If using local dependency checkouts, add their ``python`` directories to
``PYTHONPATH`` as well. Use versions compatible with this Guider checkout.

Install the Python package
----------------------------------------

With the dependencies already available, install the checkout in editable
mode to create its console commands:

.. code-block:: bash

   python -m pip install --no-deps --no-build-isolation -e .
   python -c "from lsst.ts.guider.pipeline import sensor_tracker; print(sensor_tracker.__file__)"
   run_daq_stamp_source_demo --help
   run_daq_guider_pipeline_demo --help

The printed module path should point into the selected checkout.
The ``--help`` commands do not need the DAQ binding or a running partition.
The pip options above use the active environment's dependencies and build
tools, including setuptools and setuptools-scm; they do not install them.
Repeat the editable install after recreating a development container.

Python tests without a DAQ connection
----------------------------------------

The following tests exercise detection, tracking, camera transformations,
offset combination, streaming coordination, and demo handling:

.. code-block:: bash

   env -u CCSDAQ -u GUIDER_RUN_EMULATOR python -m pytest -q \
       tests/test_guider_tracker_config.py tests/test_centroid_measurement.py \
       tests/test_detection.py tests/test_sensor_tracker.py \
       tests/test_guider_orientation.py tests/test_offset_combiner.py \
       tests/test_multi_sensor_runner.py tests/test_daq_stamp_source_demo.py \
       tests/test_sensor_identifiers.py tests/test_streaming_guider_processor.py \
       tests/test_daq_guider_pipeline_demo.py tests/test_daq_emulator.py

They need the LSST Python environment and pytest, but no live DAQ partition
or Kafka service. ``env -u CCSDAQ`` prevents the repository's pytest setup
hook from trying to build the binding when it is absent. It affects only
this command; the shell's SDK setting is retained.
The command also unsets ``GUIDER_RUN_EMULATOR``, so ``test_daq_emulator.py``
runs its cleanup checks and skips the live integration case. See the opt-in
command below for a run with actual DAQ traffic.

The full repository suite also includes CSC tests, which need the salobj
test environment, Kafka services, and compatible XML and configuration
packages. Run those in the configured T&S development environment.

C++ tests without the SDK
----------------------------------------

CMake, a C++ compiler, and GoogleTest are sufficient for the stamp model
tests. In the LSST development environment, GoogleTest is already available.
Run from the repository root:

.. code-block:: bash

   cmake -S cpp/cmake -B cpp/cmake/build_sdk_free \
       -DBUILD_MODULE=OFF -DBUILD_TESTS=ON
   cmake --build cpp/cmake/build_sdk_free -j2
   ctest --test-dir cpp/cmake/build_sdk_free --output-on-failure

``BUILD_MODULE=OFF`` avoids the SDK and pybind11 requirements. These tests
check ``Stamp`` and ``StampMetadata`` values, callback value transfer, and
pixel indexing. They do not compile the DAQ decoder or exercise the Python
binding. ``tests/test_cpp_unit_tests.py`` runs this build from pytest too.

The two CMake switches are independent: ``BUILD_MODULE`` defaults to
``ON`` and ``BUILD_TESTS`` to ``OFF``. Keep separate build directories for
SDK-free tests and the DAQ extension so their cached settings are clear.

Select the DAQ SDK
----------------------------------------

For the extension, also provide pybind11 and an unpacked SDK containing
``include/gds/Decoder.hh`` and ``x86/lib``. The development recipe uses
R5-V13.16. Set the SDK path before running CMake:

.. code-block:: bash

   export CCSDAQ=/path/to/R5-V13.16
   export PATH="$CCSDAQ/x86/bin:$PATH"
   export LD_LIBRARY_PATH="$HOME/readline-shim:$CCSDAQ/x86/lib:${LD_LIBRARY_PATH:-}"

``CCSDAQ`` selects the SDK for compilation. ``PATH`` exposes its programs,
including ``dsid_standalone`` and ``gds_emulator``. ``LD_LIBRARY_PATH`` lets
the extension and those programs find the SDK's shared libraries and any
compatibility aliases under ``$HOME/readline-shim``.
Set these variables in every terminal that uses the DAQ components.

Some SDK releases require older shared-library filenames. Inspect the
runtime dependencies if an SDK program fails to start:

.. code-block:: bash

   ldd "$CCSDAQ/x86/bin/gds_emulator"

The container recipe below supplies the library aliases used by the
original development environment. Use them only with SDK and conda
libraries whose binary compatibility has been verified for that setup;
creating an alias does not itself establish compatibility.
Resolve missing runtime libraries before importing the binding. If the
verified development aliases are needed, create them using the
:ref:`stamp-source setup instructions <daq-stamp-source-demo>`.

Build and check the DAQ binding
----------------------------------------

Use the same activated Python environment for the build and the tests:

.. code-block:: bash

   cmake -S cpp/cmake -B cpp/cmake/build_v16 \
       -DBUILD_MODULE=ON -DBUILD_TESTS=OFF \
       -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF
   cmake --build cpp/cmake/build_v16 -j2
   export PYTHONPATH="$GUIDER_REPO/cpp/cmake/build_v16:$PYTHONPATH"
   python -c "import guiderGDS; print(guiderGDS.__file__)"
   python -m pytest -q -rs tests/test_guiderGDS_bindings.py

This development build disables optional link-time optimization. An
earlier build in the conda GCC 14.3.0 environment failed in that optional
compiler pass. The setting avoids that failure without requiring a
different compiler.

The extension remains in ``cpp/cmake/build_v16``; the editable Python
install does not build or install it. Check that the printed import path
points into this checkout's build directory. Use a fresh build directory
when changing the SDK or Python environment, and rebuild after changing
the C++ sources.

The binding tests send synthetic pixels and metadata through the real
C++/Python callback using ``_invoke_callback_for_test``. They require an
importable extension and its runtime libraries, but no running DAQ service.
They skip if the extension cannot be imported. A skip is not a successful
binding check; use the import diagnostic above and inspect the reasons
printed by ``-rs``.

The pytest setup hook can build a missing binding when ``CCSDAQ`` is set.
An explicit build is preferable for diagnosis because it shows compiler
and linker errors directly and lets you verify which module is loaded.

Prepare for live DAQ traffic
----------------------------------------

The scripts in ``scripts/`` perform different parts of local emulator
setup. Run them from the repository root, in the environment listed here:

.. list-table:: Setup recipes
   :header-rows: 1
   :widths: 40 25 35

   * - Script
     - Where to run it
     - What it prepares
   * - ``setup_guider_emulator_container.sh``
     - Inside the Linux container, on either host platform.
     - The ``lsst-daq`` dummy interface and the user's SDK library aliases.
   * - ``tune_linux_guider_udp.sh``
     - On a Linux host, outside the container.
     - Host UDP receive-buffer limits and network backlog.
   * - ``tune_mac_guider_udp.sh``
     - On the Mac host with Docker Desktop running.
     - The same UDP settings in Docker Desktop's Linux VM.

For a new container with an activated conda environment, ``sudo``,
``iproute2``, and network administration privileges:

.. code-block:: bash

   bash scripts/setup_guider_emulator_container.sh
   export LD_LIBRARY_PATH="$HOME/readline-shim:$LD_LIBRARY_PATH"

Run this as the user who will run the SDK programs: the aliases are
created under that user's home directory. The ``lsst-daq`` interface must
not already exist. The script stops on a failed command, so it is a recipe
for a fresh container rather than a command to repeat at every shell start.

The stock ``lsstts/develop-env`` image has no ``sudo``. In that case, use
the :ref:`stamp-source guide <daq-stamp-source-demo>` procedure: create
the interface in a root container shell, then create the library aliases
as ``saluser``. The container needs ``NET_ADMIN`` capability for the
interface operation. Recreate the interface and aliases when the container
is recreated, and export the library path in each working shell.

Choose the UDP tuning command for the host platform. On Linux:

.. code-block:: bash

   bash scripts/tune_linux_guider_udp.sh

On macOS, with Docker Desktop's local context selected:

.. code-block:: bash

   bash scripts/tune_mac_guider_udp.sh

The Linux command uses ``sudo sysctl`` and changes host settings until
reboot. The Mac command uses a privileged helper container and changes
VM-wide settings until Docker Desktop restarts. Both set receive-buffer
limits to 134217728 bytes and the network backlog to 5000. Neither starts
the guider, a partition service, or an emulator.

Decoder and emulator validation
----------------------------------------

The SDK-free and binding tests above do not verify that the decoder reads
ROI metadata from START or image labels from RESUME. The decoder tests
call the actual C++ callbacks to check per-sensor metadata, image changes,
new series, and delivered pixels. They also check that an incorrect stamp
size prevents delivery and a consumer exception propagates. They require
the SDK and a running test partition because the SDK decoder constructor
looks up that partition. They do not receive emulator traffic.

Build the decoder tests with both CMake switches enabled:

.. code-block:: bash

   cmake -S cpp/cmake -B cpp/cmake/build_decoder \
       -DBUILD_MODULE=ON -DBUILD_TESTS=ON \
       -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF
   cmake --build cpp/cmake/build_decoder -j2

This builds the binding, stamp model tests, and ``test_decoder``. With
``BUILD_MODULE=OFF``, only the SDK-free stamp model tests are built.

In another terminal with the SDK environment activated, start an owned
test partition on the prepared interface. Use a unique partition name and
a temporary directory for its files:

.. code-block:: bash

   GUIDER_TEST_SITE=$(mktemp -d)
   dsid_standalone -i lsst-daq -p guider-decoder-test -s "$GUIDER_TEST_SITE"

Once the service is ready, run the tests from the repository root in the
build terminal, using the same partition name:

.. code-block:: bash

   GUIDER_TEST_PARTITION=guider-decoder-test \
       ctest --test-dir cpp/cmake/build_decoder --output-on-failure

An unset or empty ``GUIDER_TEST_PARTITION`` makes the decoder tests report
``Skipped``; the stamp model tests still run. Check that the decoder cases
actually passed. With a partition name set, SDK lookup or initialization
errors fail the test instead of skipping it. CTest limits each decoder
case to 20 seconds.
Stop the service with Ctrl-C in its terminal when testing is complete.
If using an existing test partition, omit the service command and set
``GUIDER_TEST_PARTITION`` to its name instead.

The opt-in emulator test exercises the full path from DAQ traffic through
the binding, tracker, and camera-coordinate combination. After preparing
the network and runtime libraries and building the binding, run:

.. code-block:: bash

   export PYTHONPATH="$GUIDER_REPO/cpp/cmake/build_decoder:$PYTHONPATH"
   python -c "import guiderGDS; print(guiderGDS.__file__)"
   GUIDER_EMULATOR_RESULTS=$(mktemp -d)
   GUIDER_RUN_EMULATOR=1 python -m pytest -q -rs tests/test_daq_emulator.py \
       --basetemp="$GUIDER_EMULATOR_RESULTS/pytest"

The test creates its own partition service, using a name derived from the
test process ID, and runs the SDK's ``gds_emulator``. It generates two FITS
inputs with equal stamp dimensions and known star motion. The 48 received
stamps cover three series, with sequence numbers ``65535``, ``0``, and ``1``.
Assertions check pixel values, camera offsets, reference selection in each
series, and changed ROI origins and labels. This SDK's FITS emulator sends
START, STAMP, and STOP; the separate decoder tests cover RESUME metadata.

No separately started partition service or FITS input is needed for this
test. It uses the prepared ``lsst-daq`` interface by default and does not
create interfaces or change host settings. ``GUIDER_EMULATOR_INTERFACE``
can select another interface for the partition service, provided the SDK
receiver is configured consistently. To run the C++ decoder cases against
the same owned partition too, set ``GUIDER_DECODER_TEST`` to the absolute
path of ``cpp/cmake/build_decoder/tests/test_decoder``.

The test stops and joins its stamp source before normal finalization and
always attempts to stop its own partition service. Processing or shutdown
failures prevent finalization; cleanup failures are attached to the
original error. Service and emulator logs are retained under the chosen
results directory, along with ``results.json`` after a successful run.
When explicitly enabled, a missing binding, SDK program, or working DAQ
setup fails the test instead of skipping it. Confirm that the live case
passed; a successful build or an intentional skip does not validate traffic.

For a manual service/subscriber/emulator session, see the
:ref:`stamp-source guide <daq-stamp-source-demo>` and
:ref:`live pipeline demo <daq-guider-pipeline-demo>`.

Build the documentation
----------------------------------------

Documentation builds need Documenteer and its Sphinx dependencies,
provided by the project's ``dev`` extra (``documenteer[pipelines]``).
Keep the Python package installed so Documenteer can read its package
metadata, and run from the repository root:

.. code-block:: bash

   GUIDER_DOCS_BUILD=$(mktemp -d)
   python -m sphinx -b html -W --keep-going doc "$GUIDER_DOCS_BUILD"

Sphinx imports the Python API, so the LSST Python dependencies must also
be available. The build does not require a DAQ subscription. The command
treats documentation warnings as errors and writes HTML outside the source
tree.
