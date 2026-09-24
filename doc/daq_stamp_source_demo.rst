.. _daq-stamp-source-demo:

DAQ stamp source demo
========================================

The ``run_daq_stamp_source_demo`` command receives DAQ stamps, logs their
pixel dimensions and sensor metadata, and stops after a requested number of
stamps or a timeout.
The defaults preserve the original demo: partition ``gds-emu``, 400 stamps,
and a 100-second timeout. The default log level is ``INFO``.
This exercises the DAQ Python bindings; it does not calculate guider offsets.

Use ``--log-level`` to select ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, or
``CRITICAL`` (case-insensitive). Stamp metadata and the final count use
``INFO``; a timeout uses ``WARNING``. Logging writes to standard error.
For example, ``--log-level WARNING`` suppresses individual stamp messages.

Workspace and development container
----------------------------------------

Use a workspace containing the ``ts_guider`` checkout, the unpacked DAQ SDK,
and the emulator FITS files. The commands below mount this workspace as
``/home/saluser/ts_repos`` inside ``lsstts/develop-env:develop``.

If you already have a development container with this mount, use its name
in the commands below. Otherwise, start one from the host::

    GUIDER_WORKSPACE=/path/to/your/workspace
    docker run --platform linux/amd64 -it --rm \
        --cap-add=NET_ADMIN \
        --name develop_env_guider \
        -v "${GUIDER_WORKSPACE}:/home/saluser/ts_repos" \
        lsstts/develop-env:develop

``NET_ADMIN`` permits creating the ``lsst-daq`` network interface.
``--platform linux/amd64`` selects the architecture of the DAQ SDK binaries
on Apple silicon; it can be omitted on an x86-64 host.
The stock image has no ``sudo``, so the interface setup below uses a root
shell. The demo uses the DAQ directly and does not require Kafka services.

Download the `R5-V13.16 DAQ SDK
<https://repo-nexus.lsst.org/nexus/repository/daq/daq-sdk/R5-V13.16.tgz>`_
and unpack it in the workspace as ``R5-V13.16``. It provides
``dsid_standalone`` and ``gds_emulator`` in ``x86/bin``.
Place the emulator data in ``guider_fits_emulator`` in the same workspace.
These FITS files need a metadata-only primary HDU and one RICE-compressed
extension per stamp.

Environment in every terminal
----------------------------------------

Open each working terminal from the host and activate the development
environment as ``saluser``::

    docker exec -it develop_env_guider bash
    source ~/.setup_dev.sh

In that shell, set the checkout and SDK paths. Adjust ``GUIDER_REPO`` to
the checkout you are testing::

    export GUIDER_REPO="$HOME/ts_repos/ts_guider"
    export DAQ_SDK="$HOME/ts_repos/R5-V13.16"
    export CCSDAQ="$DAQ_SDK"
    export PATH="$DAQ_SDK/x86/bin:$PATH"
    export LD_LIBRARY_PATH="$HOME/readline-shim:$DAQ_SDK/x86/lib:${LD_LIBRARY_PATH:-}"
    export PYTHONPATH="$GUIDER_REPO/python:$GUIDER_REPO/cpp/cmake/build_v16:${PYTHONPATH:-}"

``CCSDAQ`` selects the SDK used by CMake. ``PATH`` exposes its executables,
``LD_LIBRARY_PATH`` exposes its shared libraries and the compatibility
shims, and ``PYTHONPATH`` exposes this checkout and its compiled binding.
The LSST environment supplies salobj and ts_xml, which are imported with
``lsst.ts.guider`` even when running this standalone demo.

If you are also testing local salobj or ts_xml checkouts, prepend their
``python`` directories to ``PYTHONPATH`` in each terminal. Use versions
compatible with the Guider checkout.

One-time container setup
----------------------------------------

Create the dummy network interface from a separate host terminal. Only
this step needs a root shell inside the container::

    docker exec -u root -it develop_env_guider bash
    ip link add lsst-daq type dummy
    ip addr add 192.168.100.1/24 dev lsst-daq
    ip link set lsst-daq up multicast on
    ip link set lsst-daq mtu 9000
    exit

Back in a ``saluser`` terminal with the development environment activated,
create the compatibility shims under that user's home directory::

    mkdir -p "$HOME/readline-shim"
    ln -sf "$CONDA_PREFIX/lib/libreadline.so.8" \
        "$HOME/readline-shim/libreadline.so.7"
    ln -sf "$CONDA_PREFIX/lib/libcfitsio.so" \
        "$HOME/readline-shim/libcfitsio.so.7"
    ln -sf "$CONDA_PREFIX/lib/libbz2.so.1.0" \
        "$HOME/readline-shim/libbz2.so.1.0"

The interface and shims must be recreated when the container is recreated.

UDP receive-buffer tuning
~~~~~~~~~~~~~~~~~~~~~~~~~

A 400-by-400 stamp of 32-bit pixels is about 640 KB. Small kernel UDP
receive buffers can drop a stamp burst before the decoder receives it.
Raise these limits on the host before running the emulator.

On macOS, the settings live in the Docker Desktop Linux VM. Run on the
Mac host::

    docker run --platform linux/amd64 --rm --privileged --pid=host alpine \
      nsenter -t 1 -m -u -i -n -p -- \
      sh -c 'sysctl -w net.core.rmem_max=134217728 \
                      net.core.rmem_default=134217728 \
                      net.core.netdev_max_backlog=5000'

These settings must be reapplied after Docker Desktop restarts.
On a Linux host, set them directly::

    sudo sysctl -w net.core.rmem_max=134217728 \
        net.core.rmem_default=134217728 \
        net.core.netdev_max_backlog=5000

To persist the Linux settings across reboots, add them to
``/etc/sysctl.d/90-daq-udp.conf``.

Build the binding and install the demo
----------------------------------------

In a ``saluser`` terminal with the environment above, build ``guiderGDS``
against the SDK selected by ``CCSDAQ``::

    cd "$GUIDER_REPO"
    cmake -S cpp/cmake -B cpp/cmake/build_v16 -DBUILD_TESTS=ON
    cmake --build cpp/cmake/build_v16 -j
    ctest --test-dir cpp/cmake/build_v16 --output-on-failure

The development environment provides CMake, pybind11, and GoogleTest.
``BUILD_TESTS`` defaults to ``OFF`` for a new build directory; specifying
``ON`` also builds the C++ unit tests. Use ``-DBUILD_TESTS=OFF`` and omit
``ctest`` if you only need the binding.

The build produces ``cpp/cmake/build_v16/guiderGDS.cpython-*.so``.
Build on a fresh checkout and rebuild after changing ``cpp/cmake/src``.
The build directory is in the mounted workspace and survives container
recreation.

Install the Python package to create the demo's console entry point::

    python -m pip install --no-deps --no-build-isolation -e .
    run_daq_stamp_source_demo --help

Repeat the editable install after recreating the container; the console
command is installed in the container's Python environment.
The help command does not require the ``guiderGDS`` extension.
Before receiving stamps, check that Python can import the built module::

    python -c "import guiderGDS; print(guiderGDS.__file__)"

The printed path should point into this checkout's ``cpp/cmake/build_v16``.
If import fails, check ``CCSDAQ``, the build output, ``PYTHONPATH``, and
``LD_LIBRARY_PATH``. The demo reports these requirements and retains the
original import error as its cause.

Run the demo in three terminals
----------------------------------------

Prepare all three terminals with the environment above. Start the partition
service first, then the demo, then the emulator.

Terminal 1: keep the DAQ partition service running::

    dsid_standalone -p gds-emu

Terminal 2: start the stamp-source demo and leave it waiting for stamps::

    run_daq_stamp_source_demo --partition gds-emu \
        --max-stamps 400 --timeout 100 --log-level INFO

Terminal 3: feed the emulator data after the demo is listening::

    cd ~/ts_repos
    gds_emulator gds-emu 600 guider_fits_emulator/MC_O_20260429_000027_*.fits

Replace the file pattern with your emulator FITS files. The partition name
must match in all three terminals. Run terminal 3 within the demo's timeout,
or increase ``--timeout`` while preparing the emulator.

For the 400-by-400 example data, output at ``INFO`` includes messages such
as ``pixels.shape: (400, 400) ...``, followed by
``received 400 stamps in Python`` once the requested count is reached.
Messages have logging level and logger prefixes and are written to standard
error. A timeout reports the number actually received and stops the stream.
Ctrl-C while waiting also stops the stream.

Troubleshooting
----------------------------------------

* ``run_daq_stamp_source_demo: command not found``: activate the development
  environment and repeat the editable install, especially after recreating
  the container.
* ``libreadline.so.7: cannot open shared object file``: recreate the shims
  as ``saluser`` and check ``LD_LIBRARY_PATH`` in the failing terminal.
* ``Cannot import guiderGDS``: check the binding build and the import-path
  diagnostic above; the chained import error identifies a missing module
  or shared library.
* No stamps received: check the common partition name, terminal startup
  order, ``lsst-daq`` interface, emulator FITS paths, and UDP buffer settings.

Checking the Python bindings
----------------------------------------

The binding tests exercise the C++/Python boundary without a live DAQ
connection, using ``_invoke_callback_for_test`` to pass a synthetic stamp
through the real callback path. With the binding built and the environment
above configured, run from the repository root::

    cd "$GUIDER_REPO"
    python -m pytest tests/test_guiderGDS_bindings.py

The tests skip if the extension cannot be imported. A skip therefore does
not confirm that the binding works; check the import diagnostic first.

API reference
----------------------------------------

.. autofunction:: lsst.ts.guider.demo.run_daq_stamp_source_demo.run_daq_stamp_source_demo
