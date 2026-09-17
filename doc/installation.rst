.. _guider-installation:

Installing the DAQ guider pipeline
========================================

The Python algorithm requires the standard LSST environment, including
``ts_salobj``, ``ts_xml``, ``ts_config_ocs``, and ``obs_lsst``.
The live source additionally requires the DAQ SDK and the ``guiderGDS``
C++ extension. These instructions consolidate the original bare-metal
AlmaLinux 9 recipe and the development-container setup.

Environment and SDK
----------------------------------------

On a configured development container::

    source /opt/lsst/software/stack/loadLSST.bash
    setup lsst_distrib

On a bare-metal machine, activate your installed LSST stack instead::

    source ~/lsst_stack/loadLSST.sh
    setup obs_lsst
    setup ts_salobj
    setup ts_xml

The original bare-metal recipe used the w_2026_25 stack. The machine
needs a C++ compiler, CMake, and the SDK's runtime libraries, including
readline, cfitsio, and bzip2. On AlmaLinux these system packages can be
installed with ``dnf`` if they are not already present::

    sudo dnf install gcc-c++ make cmake git bzip2 readline cfitsio

Unpack the DAQ SDK separately and select its location before building::

    export CCSDAQ=/path/to/R5-V13.16
    export PATH="$CCSDAQ/x86/bin:$PATH"
    export LD_LIBRARY_PATH="$CCSDAQ/x86/lib:$LD_LIBRARY_PATH"

R5-V13.16 provides the emulator options used in this guide, including
multiple sensor files, ``-n`` (stamp limit), ``-l`` (loops), and ``-q``
(initial sequence). Use ``gds_emulator -h`` to inspect the installed SDK.

Some SDK builds reference older shared-library filenames. Inspect
``ldd "$CCSDAQ/x86/bin/gds_emulator"`` if startup reports a missing library.
The original development setup uses these compatibility links with the
activated LSST conda environment::

    mkdir -p ~/readline-shim
    ln -sf "$CONDA_PREFIX/lib/libreadline.so.8" ~/readline-shim/libreadline.so.7
    ln -sf "$CONDA_PREFIX/lib/libcfitsio.so" ~/readline-shim/libcfitsio.so.7
    ln -sf "$CONDA_PREFIX/lib/libbz2.so.1.0" ~/readline-shim/libbz2.so.1.0
    export LD_LIBRARY_PATH="$HOME/readline-shim:$LD_LIBRARY_PATH"

Build and install
----------------------------------------

From the ts_guider checkout::

    cmake -S cpp/cmake -B cpp/cmake/build -DBUILD_TESTS=ON
    cmake --build cpp/cmake/build -j2
    export PYTHONPATH="$PWD/cpp/cmake/build:$PYTHONPATH"
    python -m pip install --no-deps --no-build-isolation -e .
    python -c "import guiderGDS; print(guiderGDS.__doc__)"
    run_daq_guider_pipeline_demo --help

The Python console command is installed through ``pyproject.toml``;
the extension remains in the CMake build directory.
If the conda GCC compiler reports an internal error in the LTO ``static-var``
pass, the validation build can disable interprocedural optimization::

    cmake -S cpp/cmake -B cpp/cmake/build \
        -DBUILD_TESTS=ON -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF
    cmake --build cpp/cmake/build -j2

Container network setup
----------------------------------------

The SDK's default DAQ interface is ``lsst-daq``. For local emulation in
a container without that interface::

    sudo ip link add lsst-daq type dummy
    sudo ip addr add 192.168.100.1/24 dev lsst-daq
    sudo ip link set lsst-daq up multicast on
    sudo ip link set lsst-daq mtu 9000

``setup_container.sh`` retains these commands and the library links from
OSW-2858. It is a one-time recipe for a container without the interface;
inspect existing interfaces before running it again.
Production hosts may already have a DAQ interface and partition service.

Large stamp bursts can exceed the Linux UDP receive buffers.
The original :ref:`stamp-source setup <daq-stamp-source-demo>` describes
buffer tuning. ``setup_mac_guider_container.sh`` applies that tuning to
the Docker Desktop Linux VM through a privileged helper container.
It changes VM-wide kernel settings and is separate from building or
running the Python package. These settings reset when Docker Desktop restarts.

On a Linux host the equivalent settings are::

    sudo sysctl -w net.core.rmem_max=134217728
    sudo sysctl -w net.core.rmem_default=134217728
    sudo sysctl -w net.core.netdev_max_backlog=5000

See :ref:`daq-guider-pipeline-demo` to start an isolated emulator partition
and run the complete algorithm. For an already-provisioned partition,
start only the subscriber and the appropriate producer.
