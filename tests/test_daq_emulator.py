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

import json
import os
import pathlib
import shutil
import subprocess
import threading
import time

import numpy as np
import pytest
from astropy.io import fits
from lsst.ts.guider.pipeline import GuiderTrackerConfig, StreamingGuiderProcessor


@pytest.mark.skipif(
    os.environ.get("GUIDER_RUN_EMULATOR") != "1",
    reason="Set GUIDER_RUN_EMULATOR=1 to run the DAQ emulator integration test.",
)
def test_decoder_and_pipeline_across_emulator_series(tmp_path):
    """Exercise the decoder, worker callback, and pipeline on DAQ traffic.

    Generates two sensor sequences with known offsets, runs two emulator loops
    through sequence rollover, then publishes new ROI/image metadata.
    Owns and stops only the partition service and emulator it starts.
    """
    import guiderGDS

    sdk = pathlib.Path(os.environ["CCSDAQ"])
    partition = f"osw2952-{os.getpid()}"
    interface = os.environ.get("GUIDER_EMULATOR_INTERFACE", "lsst-daq")
    paths = []
    expected_roi = {}
    expected_pixels = {}
    camera_offsets = [(0, 0)] * 5 + [(2, -1), (0.4, -0.65), (-1.2, 2.3)]
    rows, columns = np.indices((100, 140))
    for sensor, sensor_index in [("R00_SG0", 3), ("R04_SG0", 39)]:
        raft, slot = sensor.split("_")
        primary = fits.PrimaryHDU()
        primary.header.update(
            {
                "ROIROWS": 100,
                "ROICOLS": 140,
                "ROIMILLI": 200,
                "RAFTBAY": raft,
                "CCDSLOT": slot,
                "ROISEG": "Segment05",
                "ROIROW": sensor_index + 10,
                "ROICOL": sensor_index + 20,
                "GDSSEQ": 1,
                "ROICCDTY": 0,
                "ROIUNDRC": 0,
                "ROIOVERC": 0,
                "ROIOVERR": 0,
                "ROIFLUSH": 2,
                "ROISPLIT": False,
                "FIRMWARE": "2139210d",
                "CONTNUM": "10000",
                "PLATFORM": "lsstcam",
                "OBSID": "osw2952-initial-roi",
                "N_STAMPS": 8,
            }
        )
        hdus = [primary]
        stamps = []
        for index, (dx, dy) in enumerate(camera_offsets):
            lx, ly = (-dy, -dx) if sensor_index == 3 else (dx, -dy)
            star = (
                100_000
                / (8 * np.pi)
                * np.exp(-((columns - 70.25 - lx) ** 2 + (rows - 45.7 - ly) ** 2) / 8)
            )
            pixels = np.rint(
                100 + star + np.random.default_rng(index).normal(0, 5, rows.shape)
            ).astype(np.int32)
            stamps.append(pixels)
            hdu = fits.CompImageHDU(pixels, compression_type="RICE_1")
            hdu.header["STMPTMJD"] = 60000 + index / 432000
            hdus.append(hdu)
        template = tmp_path / f"{sensor}.fits"
        fits.HDUList(hdus).writeto(template)
        paths.append(template)
        expected_pixels[sensor_index] = stamps
        expected_roi[sensor_index] = (5, sensor_index + 10, sensor_index + 20)

    visits, starts, records, errors = [], [], [], []
    received = threading.Event()
    processor = StreamingGuiderProcessor(
        GuiderTrackerConfig(seed_frames=5, min_snr=5),
        on_visit_start=lambda sequence, label: starts.append((sequence, label)),
        on_visit_complete=lambda sequence, offsets: visits.append((sequence, offsets)),
    )
    processor.combiner.orientation.camera

    def on_stamp(pixels, metadata):
        try:
            records.append(
                {
                    name: getattr(metadata, name)
                    for name in (
                        "sequence",
                        "stamp_index",
                        "sensor_index",
                        "segment",
                        "startrow",
                        "startcol",
                        "obs_id",
                        "series_id",
                    )
                }
            )
            assert pixels.shape == (100, 140)
            assert pixels.dtype == np.int32
            np.testing.assert_array_equal(
                pixels, expected_pixels[metadata.sensor_index][metadata.stamp_index - 1]
            )
            processor.on_stamp(pixels, metadata)
        except Exception as error:
            errors.append(error)
        received.set()

    service_log = tmp_path / "partition.log"
    # Line-buffer the SDK utility so startup errors are available on failure.
    command = [
        str(sdk / "x86/bin/dsid_standalone"),
        "-i",
        interface,
        "-p",
        partition,
        "-s",
        str(tmp_path / "site"),
    ]
    if shutil.which("stdbuf"):
        command = ["stdbuf", "-oL", "-eL", *command]
    source = None
    with service_log.open("w") as output:
        service = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        try:
            # The standalone service registers the partition before subscribers
            # can look it up. Give startup a bounded settling interval.
            time.sleep(1)
            assert service.poll() is None, service_log.read_text()
            decoder_test = os.environ.get("GUIDER_DECODER_TEST")
            if decoder_test:
                result = subprocess.run(
                    [decoder_test],
                    env=dict(os.environ, GUIDER_TEST_PARTITION=partition),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                (tmp_path / "decoder.log").write_text(result.stdout + result.stderr)
                assert result.returncode == 0, result.stdout + result.stderr
            source = guiderGDS.DaqStampSource(
                partition=partition, locations=guiderGDS.LocationSet.any()
            )
            source.start_stamp_stream(on_stamp)
            for sequence, loops, label, expected_count in [
                (65535, 2, "osw2952-first", 32),
                (1, 1, "osw2952-second", 48),
            ]:
                current_paths = paths
                if sequence == 1:
                    current_paths = []
                    for path in paths:
                        with fits.open(path) as hdus:
                            hdus[0].header["ROIROW"] += 10
                            hdus[0].header["ROICOL"] += 20
                            hdus[0].header["OBSID"] = "osw2952-new-roi"
                            changed = tmp_path / f"changed-{path.name}"
                            hdus.writeto(changed)
                            current_paths.append(changed)
                result = subprocess.run(
                    [
                        str(sdk / "x86/bin/gds_emulator"),
                        "-t",
                        "2000",
                        "-q",
                        str(sequence),
                        "-l",
                        str(loops),
                        "-n",
                        "8",
                        "-c",
                        label,
                        partition,
                        "200",
                        *map(str, current_paths),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=30,
                )
                (tmp_path / f"emulator-{sequence}.log").write_text(result.stdout)
                assert result.returncode == 0, result.stdout
                deadline = time.monotonic() + 5
                while len(records) < expected_count and time.monotonic() < deadline:
                    received.wait(0.1)
                    received.clear()
                assert not errors, repr(errors)
                assert len(records) == expected_count, (
                    result.stdout + service_log.read_text()
                )
        finally:
            if source is not None:
                source.stop_stamp_stream()
            processor.finalize()
            (tmp_path / "results.json").write_text(
                json.dumps(
                    {
                        "records": records,
                        "starts": starts,
                        "visits": [
                            (
                                sequence,
                                [
                                    {
                                        "stamp_index": offset.stamp_index,
                                        "dx": offset.combined_dx,
                                        "dy": offset.combined_dy,
                                        "n_valid": offset.n_valid,
                                    }
                                    for offset in offsets
                                ],
                            )
                            for sequence, offsets in visits
                        ],
                        "lock_times": processor.metrics.lock_times,
                        "combine_times": processor.metrics.combine_times,
                    },
                    indent=2,
                )
            )
            service.terminate()
            try:
                service.wait(timeout=5)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=5)

    processor.finalize()
    assert [sequence for sequence, _ in starts] == [65535, 0, 1]
    # This SDK's FITS reader supplies START/STAMP/STOP without RESUME.
    # The processor uses series_id as its label until a RESUME arrives.
    assert [label for _, label in starts] == [
        "osw2952-initial-roi",
        "osw2952-initial-roi",
        "osw2952-new-roi",
    ]
    assert [sequence for sequence, _ in visits] == [65535, 0, 1]
    for _, offsets in visits:
        assert [offset.stamp_index for offset in offsets] == list(range(1, 9))
        assert any(offset.n_valid == 2 for offset in offsets)
        np.testing.assert_allclose(
            [(offset.combined_dx, offset.combined_dy) for offset in offsets],
            camera_offsets,
            atol=0.05,
        )
    for metadata in records:
        segment, row, col = expected_roi[metadata["sensor_index"]]
        if metadata["sequence"] == 1:
            row += 10
            col += 20
            assert metadata["series_id"] == "osw2952-new-roi"
        else:
            assert metadata["series_id"] == "osw2952-initial-roi"
        assert metadata["obs_id"] == ""
        assert (metadata["segment"], metadata["startrow"], metadata["startcol"]) == (
            segment,
            row,
            col,
        )
        assert metadata["series_id"]
    assert not processor.acquisitions
    assert not errors
