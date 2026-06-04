"""End-to-end demo for guiderGDS.DaqStampSource.

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
    ln -sf $CONDA_PREFIX/lib/libreadline.so.8 \\
           ~/readline-shim/libreadline.so.7
    ln -sf $CONDA_PREFIX/lib/libcfitsio.so  \\
           ~/readline-shim/libcfitsio.so.7
    ln -sf $CONDA_PREFIX/lib/libbz2.so.1.0 \\
           ~/readline-shim/libbz2.so.1.0

UDP receive-buffer tuning
~~~~~~~~~~~~~~~~~~~~~~~~~
Each decoded guider stamp is ~640 KB.  The default Linux kernel
UDP receive buffer (rmem_max = 208 KB) cannot queue a full stamp
burst, causing fragment loss and zero delivered stamps.

On **macOS with Docker Desktop** — containers run inside a
LinuxKit VM whose ``/proc/sys/`` is read-only from inside the
container.  Set the values in the VM kernel from the Mac host::

    docker run --rm --privileged --pid=host alpine \\
      nsenter -t 1 -m -u -i -n -p -- \\
      sh -c 'sysctl -w net.core.rmem_max=134217728 \\
                      net.core.rmem_default=134217728 \\
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

    cd /home/saluser/ts_repos/ts_guider/cpp/cmake
    PYTHONPATH=build_v16 python run_daq_stamp_source_demo.py

Terminal 3 — emulator (run after terminal 2 is listening)::

    gds_emulator gds-emu 200 /path/to/guider.fits

    e.g. (run from ``/home/saluser/ts_repos``, or use absolute paths)::

    E.g.
    gds_emulator gds-emu 600 \\
      guider_fits_emulator/MC_O_20260429_000027_R00_SG0_guider.fits \\
      guider_fits_emulator/MC_O_20260429_000027_R00_SG1_guider.fits \\
      guider_fits_emulator/MC_O_20260429_000027_R04_SG0_guider.fits \\
      guider_fits_emulator/MC_O_20260429_000027_R04_SG1_guider.fits \\
      guider_fits_emulator/MC_O_20260429_000027_R40_SG0_guider.fits \\
      guider_fits_emulator/MC_O_20260429_000027_R40_SG1_guider.fits \\
      guider_fits_emulator/MC_O_20260429_000027_R44_SG0_guider.fits \\
      guider_fits_emulator/MC_O_20260429_000027_R44_SG1_guider.fits

Expected output: 400 lines of ``pixels.shape: (400, 400) ...``
followed by ``received 400 stamps in Python``.
"""

import threading

import guiderGDS
import numpy as np

PARTITION = "gds-emu"
MAX_STAMPS = 400
TIMEOUT_SECONDS = 100.0

locations = guiderGDS.LocationSet.any()

source = guiderGDS.DaqStampSource(
    partition=PARTITION,
    locations=locations,
)

received = []
done = threading.Event()


def on_stamp(pixels: np.ndarray, metadata: guiderGDS.StampMetadata) -> None:
    print(
        f"pixels.shape: {pixels.shape}, "
        f"pixels.max: {int(pixels.max())}, "
        f"metadata: {metadata}"
    )
    received.append(metadata)
    if len(received) >= MAX_STAMPS:
        done.set()


source.start_stamp_stream(on_stamp)
try:
    if not done.wait(timeout=TIMEOUT_SECONDS):
        print(f"timeout — only received {len(received)} stamps")
finally:
    source.stop_stamp_stream()
    print(f"received {len(received)} stamps in Python")
