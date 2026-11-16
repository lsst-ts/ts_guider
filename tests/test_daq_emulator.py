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
from unittest.mock import Mock, call

import numpy as np
import pytest
from astropy.io import fits
from lsst.ts.guider.pipeline import GuiderTrackerConfig, StreamingGuiderProcessor


def _finish_emulator_run(
    source, processor, write_results, service, callback_errors, error=None
):
    """Stop owned resources and raise the first failure from the run.

    Join the source before checking callback errors or finalizing. Finalize
    and write results only after a successful run and source shutdown.
    Always attempt to stop the partition service, even if an earlier step
    failed. Attach cleanup failures to the original exception as notes.
    """
    try:
        if source is not None:
            source.stop_stamp_stream()
    except BaseException as stop_error:
        if error is None:
            error = stop_error
        else:
            error.add_note(f"Stopping the stamp source also failed: {stop_error!r}")

    # A callback may have failed while the main thread was stopping the
    # source. Check again after the shutdown attempt.
    if callback_errors:
        callback_error = callback_errors[0]
        if error is not None and error is not callback_error:
            callback_error.add_note(f"The main thread also failed: {error!r}")
            for note in getattr(error, "__notes__", ()):
                callback_error.add_note(note)
        error = callback_error

    try:
        if error is None:
            processor.finalize()
            write_results()
    except BaseException as result_error:
        error = result_error
    finally:
        try:
            try:
                if service.poll() is None:
                    service.terminate()
            finally:
                try:
                    service.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=5)
        except BaseException as cleanup_error:
            if error is None:
                error = cleanup_error
            else:
                error.add_note(
                    f"Stopping the partition service also failed: {cleanup_error!r}"
                )
    if error is not None:
        raise error


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
        if errors:
            return
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

    def write_results():
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
        run_error = None
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
                while (
                    len(records) < expected_count
                    and not errors
                    and time.monotonic() < deadline
                ):
                    received.wait(0.1)
                    received.clear()
                if errors:
                    raise errors[0]
                assert len(records) == expected_count, (
                    result.stdout + service_log.read_text()
                )
        except BaseException as error:
            run_error = error
        finally:
            _finish_emulator_run(
                source, processor, write_results, service, errors, run_error
            )

    processor.finalize()
    assert len(records) == processor.metrics.total_stamps == 48
    assert len(processor.metrics.lock_times) == 6
    assert processor.combiner.sensor_amplifiers == {"R00_SG0": "C05", "R04_SG0": "C05"}
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


@pytest.fixture
def run_resources():
    """Record cleanup ordering without starting SDK processes or threads."""
    resources = Mock()
    resources.service.poll.return_value = None
    return resources


def test_successful_run_stops_source_before_finalizing(run_resources):
    r = run_resources
    _finish_emulator_run(r.source, r.processor, r.write_results, r.service, [])
    assert r.mock_calls == [
        call.source.stop_stamp_stream(),
        call.processor.finalize(),
        call.write_results(),
        call.service.poll(),
        call.service.terminate(),
        call.service.wait(timeout=5),
    ]


@pytest.mark.parametrize("stage", ["setup", "callback", "stop", "finalize", "write"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failure_still_stops_partition_and_preserves_error(
    run_resources, stage, cleanup_fails
):
    r = run_resources
    original = ValueError(f"{stage} failed")
    callback_errors = [original] if stage == "callback" else []
    source = None if stage == "setup" else r.source
    if stage == "stop":
        r.source.stop_stamp_stream.side_effect = original
    elif stage == "finalize":
        r.processor.finalize.side_effect = original
    elif stage == "write":
        r.write_results.side_effect = original
    if cleanup_fails:
        r.service.terminate.side_effect = RuntimeError("partition cleanup failed")

    with pytest.raises(ValueError) as caught:
        _finish_emulator_run(
            source,
            r.processor,
            r.write_results,
            r.service,
            callback_errors,
            original if stage == "setup" else None,
        )
    assert caught.value is original
    r.service.terminate.assert_called_once_with()
    r.service.wait.assert_called_once_with(timeout=5)
    if stage in {"setup", "callback", "stop"}:
        r.processor.finalize.assert_not_called()
    else:
        r.processor.finalize.assert_called_once_with()
    if stage != "write":
        r.write_results.assert_not_called()
    if cleanup_fails:
        assert any("partition cleanup failed" in note for note in original.__notes__)


def test_callback_error_during_shutdown_is_preserved(run_resources):
    r = run_resources
    original = ValueError("callback failed")
    callback_errors = []

    def stop():
        callback_errors.append(original)
        raise RuntimeError("source shutdown failed")

    r.source.stop_stamp_stream.side_effect = stop
    with pytest.raises(ValueError) as caught:
        _finish_emulator_run(
            r.source,
            r.processor,
            r.write_results,
            r.service,
            callback_errors,
            TimeoutError("delivery timed out"),
        )
    assert caught.value is original
    assert any("delivery timed out" in note for note in original.__notes__)
    assert any("source shutdown failed" in note for note in original.__notes__)
    r.processor.finalize.assert_not_called()
    r.write_results.assert_not_called()
    r.service.terminate.assert_called_once_with()
    r.service.wait.assert_called_once_with(timeout=5)


def test_partition_shutdown_timeout_kills_and_waits(run_resources):
    r = run_resources
    r.service.wait.side_effect = [subprocess.TimeoutExpired("partition", 5), 0]
    _finish_emulator_run(r.source, r.processor, r.write_results, r.service, [])
    assert r.service.mock_calls == [
        call.poll(),
        call.terminate(),
        call.wait(timeout=5),
        call.kill(),
        call.wait(timeout=5),
    ]


def test_exited_partition_is_reaped_without_signaling(run_resources):
    r = run_resources
    r.service.poll.return_value = 1
    _finish_emulator_run(r.source, r.processor, r.write_results, r.service, [])
    r.service.terminate.assert_not_called()
    r.service.kill.assert_not_called()
    r.service.wait.assert_called_once_with(timeout=5)


@pytest.mark.parametrize("has_processing_error", [False, True])
def test_partition_kill_failure_is_reported(run_resources, has_processing_error):
    r = run_resources
    original = ValueError("processing failed") if has_processing_error else None
    cleanup_error = RuntimeError("partition kill failed")
    r.service.wait.side_effect = subprocess.TimeoutExpired("partition", 5)
    r.service.kill.side_effect = cleanup_error
    with pytest.raises((ValueError, RuntimeError)) as caught:
        _finish_emulator_run(
            r.source, r.processor, r.write_results, r.service, [], original
        )
    assert caught.value is (original if has_processing_error else cleanup_error)
    if has_processing_error:
        assert any("partition kill failed" in note for note in original.__notes__)
        r.processor.finalize.assert_not_called()
        r.write_results.assert_not_called()
