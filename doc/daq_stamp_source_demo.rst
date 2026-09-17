.. _daq-stamp-source-demo:

DAQ stamp source demo
========================================

The ``run_daq_stamp_source_demo`` command receives DAQ stamps, prints their
pixel dimensions and sensor metadata, and stops after a requested number of
stamps or a timeout.
The defaults preserve the original demo: partition ``gds-emu``, 400 stamps,
and a 100-second timeout.
This exercises the DAQ Python bindings; it does not calculate guider offsets.
For the complete tracking algorithm, use :ref:`daq-guider-pipeline-demo`.
The printed metadata also includes the ROI origin, image name, and series ID.

Activate the standard LSST environment, including salobj and ts_xml, and
install the Python package from the repository root::

    python -m pip install --no-deps --no-build-isolation -e .
    run_daq_stamp_source_demo --help

The help command does not require the ``guiderGDS`` extension.
Receiving stamps requires the DAQ SDK and a built extension as described
below.

This is a runnable demo, not an automated test.  It requires the
GDS emulator infrastructure running inside the development
container with image: lsstts/develop-env:develop

Prerequisites
-------------
- DAQ SDK unpacked at $CCSDAQ (e.g. R5-V13.16 /
   https://repo-nexus.lsst.org/nexus/repository/daq/daq-sdk/R5-V13.16.tgz).

   E.g. export CCSDAQ=/home/saluser/ts_repos/R5-V13.16

- The guiderGDS module built::

      cd ts_guider/cpp/cmake
      cmake -S . -B build_v16
      cmake --build build_v16

- A guider FITS file in emulator format (metadata-only primary
  HDU + one RICE-compressed extension per stamp).

One-time container setup
------------------------
::

    sudo ip link add lsst-daq type dummy
    sudo ip addr add 192.168.100.1/24 dev lsst-daq
    sudo ip link set lsst-daq up multicast on
    sudo ip link set lsst-daq mtu 9000

    # Soname shims (SDK built against older libreadline)
    mkdir -p ~/readline-shim
    ln -sf $CONDA_PREFIX/lib/libreadline.so.8 \
           ~/readline-shim/libreadline.so.7
    ln -sf $CONDA_PREFIX/lib/libcfitsio.so  \
           ~/readline-shim/libcfitsio.so.7
    ln -sf $CONDA_PREFIX/lib/libbz2.so.1.0 \
           ~/readline-shim/libbz2.so.1.0

UDP receive-buffer tuning
~~~~~~~~~~~~~~~~~~~~~~~~~
Each decoded guider stamp is ~640 KB.  The default Linux kernel
UDP receive buffer (rmem_max = 208 KB) cannot queue a full stamp
burst, causing fragment loss and zero delivered stamps.

On **macOS with Docker Desktop** — containers run inside a
LinuxKit VM whose ``/proc/sys/`` is read-only from inside the
container.  Set the values in the VM kernel from the Mac host::

    docker run --rm --privileged --pid=host alpine \
      nsenter -t 1 -m -u -i -n -p -- \
      sh -c 'sysctl -w net.core.rmem_max=134217728 \
                      net.core.rmem_default=134217728 \
                      net.core.netdev_max_backlog=5000'

On a **Linux host** — set directly::

    sudo sysctl -w net.core.rmem_max=134217728
    sudo sysctl -w net.core.rmem_default=134217728
    sudo sysctl -w net.core.netdev_max_backlog=5000

To persist across reboots add them to
``/etc/sysctl.d/90-daq-udp.conf``.

Note: These values reset when Docker Desktop restarts.

Three-terminal setup (inside the container)
-------------------------------------------
All terminals need the SDK on PATH / LD_LIBRARY_PATH::

    export DAQ_SDK=/home/saluser/ts_repos/R5-V13.16
    export LD_LIBRARY_PATH=~/readline-shim:$DAQ_SDK/x86/lib:$LD_LIBRARY_PATH
    export PATH=$DAQ_SDK/x86/bin:$PATH

Terminal 1 — partition service (leave running)::

    dsid_standalone -p gds-emu

Terminal 2 — this demo::

    cd /home/saluser/ts_repos/ts_guider
    PYTHONPATH="$PWD/cpp/cmake/build_v16:$PYTHONPATH" \
        run_daq_stamp_source_demo --partition gds-emu \
        --max-stamps 400 --timeout 100

Terminal 3 — emulator (run after terminal 2 is listening)::

    gds_emulator gds-emu 200 /path/to/guider.fits

    e.g. (run from ``/home/saluser/ts_repos``, or use absolute paths)::

    E.g.
    gds_emulator gds-emu 600 \
      guider_fits_emulator/MC_O_20260429_000027_R00_SG0_guider.fits \
      guider_fits_emulator/MC_O_20260429_000027_R00_SG1_guider.fits \
      guider_fits_emulator/MC_O_20260429_000027_R04_SG0_guider.fits \
      guider_fits_emulator/MC_O_20260429_000027_R04_SG1_guider.fits \
      guider_fits_emulator/MC_O_20260429_000027_R40_SG0_guider.fits \
      guider_fits_emulator/MC_O_20260429_000027_R40_SG1_guider.fits \
      guider_fits_emulator/MC_O_20260429_000027_R44_SG0_guider.fits \
      guider_fits_emulator/MC_O_20260429_000027_R44_SG1_guider.fits

Expected output: 400 lines of ``pixels.shape: (400, 400) ...``
followed by ``received 400 stamps in Python``.
A timeout reports the number actually received and stops the stream.

Checking the Python bindings
----------------------------------------

The binding tests exercise the C++/Python boundary without a live DAQ
connection, using ``_invoke_callback_for_test`` to pass a synthetic stamp
through the real callback path.
The tests skip if the extension cannot be imported.
With the SDK and library paths configured, run from the repository root::

    cmake -S cpp/cmake -B cpp/cmake/build_v16 -DBUILD_TESTS=ON
    cmake --build cpp/cmake/build_v16
    PYTHONPATH="$PWD/cpp/cmake/build_v16:$PYTHONPATH" \
        python -m pytest tests/test_guiderGDS_bindings.py

API reference
-------------

.. autofunction:: lsst.ts.guider.demo.run_daq_stamp_source_demo
