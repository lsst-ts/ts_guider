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

import numpy as np
import pytest
from astropy.io import fits
from lsst.ts.guider.pipeline import (
    GuiderTrackerConfig,
    MultiSensorRunner,
    read_guider_sequence,
    run_offline,
)

REFERENCE = np.array([70.25, 45.7])
SHAPE = (100, 140)


def make_stamp(local_offset=(0, 0), seed=0):
    """Create a noisy integer image with a star at a known position."""
    rows, columns = np.indices(SHAPE)
    x, y = REFERENCE + local_offset
    star = (
        100_000
        / (2 * np.pi * 2**2)
        * np.exp(-((columns - x) ** 2 + (rows - y) ** 2) / 8)
    )
    image = 100 + star + np.random.default_rng(seed).normal(0, 5, SHAPE)
    return np.rint(image).astype(np.int32)


def write_sequence(path, sensor, stamps, segment="Segment05"):
    """Write the emulator's metadata primary HDU and compressed stamps."""
    raft, slot = sensor.split("_")
    primary = fits.PrimaryHDU()
    primary.header["RAFTBAY"] = raft
    primary.header["CCDSLOT"] = slot
    if segment is not None:
        primary.header["ROISEG"] = segment
    hdus = [primary]
    for index, stamp in enumerate(stamps):
        hdu = fits.CompImageHDU(stamp, compression_type="RICE_1")
        hdu.header["STMPTMJD"] = 60000.0 + index / 864000.0
        hdus.append(hdu)
    fits.HDUList(hdus).writeto(path)
    return path


@pytest.fixture
def aligned_sequences(tmp_path):
    # The first five frames establish stationary references. Later
    # displacements are specified in camera pixels and converted here by
    # hand for R00/C05 and R04/C05, independently of GuiderOrientation.
    offsets = [(0, 0)] * 5 + [(2, -1), (0.4, -0.65), (-1.2, 2.3), (1, 1)]
    sequences = {}
    paths = []
    for sensor in ("R00_SG0", "R04_SG0"):
        local = [(-dy, -dx) if sensor == "R00_SG0" else (dx, -dy) for dx, dy in offsets]
        stamps = [make_stamp(offset, seed=index) for index, offset in enumerate(local)]
        if sensor == "R04_SG0":
            stamps.append(make_stamp((3, 2), seed=99))
        sequences[sensor] = np.stack(stamps)
        paths.append(write_sequence(tmp_path / f"{sensor}.fits", sensor, stamps))
    return paths, sequences, np.array(offsets)


def test_fits_round_trip_preserves_stamps_and_metadata(aligned_sequences):
    paths, sequences, _ = aligned_sequences
    for path in paths:
        sequence = read_guider_sequence(path)
        assert sequence.sensor_name == path.stem
        assert sequence.segment == "Segment05"
        assert sequence.stamps.dtype == np.float32
        assert sequence.n_stamps == len(sequences[path.stem])
        np.testing.assert_array_equal(sequence.stamps, sequences[path.stem])
        np.testing.assert_allclose(
            sequence.timestamps_mjd,
            60000 + np.arange(sequence.n_stamps) / 864000,
            rtol=0,
            atol=1e-10,
        )


def test_offline_camera_offsets_on_compressed_fits(aligned_sequences):
    paths, _, expected = aligned_sequences
    results = run_offline(paths, GuiderTrackerConfig(seed_frames=5))
    assert len(results) == len(expected)  # The shortest sequence determines the limit.
    assert [r.stamp_index for r in results] == list(range(len(expected)))
    np.testing.assert_allclose(
        [(r.combined_dx, r.combined_dy) for r in results], expected, atol=0.05
    )
    for result, offset in zip(results, expected):
        assert (result.n_valid, result.n_total) == (2, 2)
        assert set(result.measurements) == {"R00_SG0", "R04_SG0"}
        for value in result.per_sensor_offset.values():
            np.testing.assert_allclose(value, offset, atol=0.05)
        np.testing.assert_allclose(
            (result.error_dx, result.error_dy),
            np.array([result.scatter_dx, result.scatter_dy]) / np.sqrt(2),
        )


def test_runner_preserves_rejected_measurement_and_skips_unlocked_sensor(
    aligned_sequences,
):
    _, sequences, _ = aligned_sequences
    config = GuiderTrackerConfig(seed_frames=5)
    runner = MultiSensorRunner(
        ["R00_SG0", "R04_SG0", "unlocked"], config, {"R00_SG0": "C05", "R04_SG0": "C05"}
    )
    assert runner.lock_references(sequences) == ["R00_SG0", "R04_SG0"]
    for center in runner.references.values():
        np.testing.assert_allclose(center, REFERENCE, atol=0.05)
    measurements, result = runner.process_acquisition(
        5,
        {
            "R00_SG0": sequences["R00_SG0"][5],
            "R04_SG0": np.zeros(SHAPE),
            "unlocked": np.zeros(SHAPE),
            "unknown": np.zeros(SHAPE),
        },
    )
    assert set(measurements) == {"R00_SG0", "R04_SG0"}
    assert not measurements["R04_SG0"].passed_quality
    assert result.measurements == measurements
    assert (result.n_valid, result.n_total) == (1, 2)
    np.testing.assert_allclose(
        (result.combined_dx, result.combined_dy), (2, -1), atol=0.05
    )
    assert np.isnan(result.scatter_dx)


def test_offline_empty_input_and_failed_locks(tmp_path):
    assert run_offline([]) == []
    path = write_sequence(
        tmp_path / "blank.fits", "R00_SG0", [np.zeros(SHAPE, dtype=np.int32)] * 5
    )
    assert run_offline([path]) == []


def test_fits_missing_metadata_uses_filename_and_nan_timestamp(tmp_path):
    path = tmp_path / "sensor.fits"
    fits.HDUList(
        [fits.PrimaryHDU(), fits.ImageHDU(), fits.ImageHDU(np.ones((3, 4)))]
    ).writeto(path)
    sequence = read_guider_sequence(path)
    assert sequence.sensor_name == "sensor"
    assert sequence.segment is None
    assert sequence.n_stamps == 1
    assert np.isnan(sequence.timestamps_mjd[0])


def test_fits_without_images_raises(tmp_path):
    path = tmp_path / "empty.fits"
    fits.HDUList([fits.PrimaryHDU()]).writeto(path)
    with pytest.raises(ValueError, match="No image stamps"):
        read_guider_sequence(path)
