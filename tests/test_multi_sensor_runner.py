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
    Detection,
    GuiderTrackerConfig,
    MeasurementState,
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
    assert runner.lock_references(
        {name: stamps[: config.seed_frames] for name, stamps in sequences.items()}
    ) == ["R00_SG0", "R04_SG0"]
    for reference in runner.references.values():
        np.testing.assert_allclose((reference.x, reference.y), REFERENCE, atol=0.05)
        assert reference.state == MeasurementState.NOT_SET
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
    assert measurements["R04_SG0"].state == MeasurementState.NOT_CONVERGED
    assert result.measurements == measurements
    assert (result.n_valid, result.n_total) == (1, 2)
    np.testing.assert_allclose(
        (result.combined_dx, result.combined_dy), (2, -1), atol=0.05
    )
    assert np.isnan(result.scatter_dx)


def test_offline_empty_input_and_failed_locks(tmp_path):
    assert run_offline([]) == []
    path = write_sequence(
        tmp_path / "blank.fits", "R00_SG0", [np.zeros(SHAPE, dtype=np.int32)] * 12
    )
    assert run_offline([path], GuiderTrackerConfig(seed_frames=5)) == []


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


def test_fits_incompatible_stamp_shapes_raise_with_filename(tmp_path):
    path = tmp_path / "shapes.fits"
    fits.HDUList(
        [
            fits.PrimaryHDU(),
            fits.ImageHDU(np.ones((3, 4))),
            fits.ImageHDU(np.ones((4, 3))),
        ]
    ).writeto(path)
    with pytest.raises(ValueError, match="Incompatible stamp shapes.*shapes.fits"):
        read_guider_sequence(path)


def test_fits_skips_non_image_extensions_without_realigning(tmp_path):
    path = tmp_path / "metadata.fits"
    fits.HDUList(
        [
            fits.PrimaryHDU(),
            fits.ImageHDU(),
            fits.ImageHDU(np.ones(5)),
            fits.ImageHDU(np.ones((3, 4))),
            fits.ImageHDU(np.full((3, 4), 2)),
        ]
    ).writeto(path)
    sequence = read_guider_sequence(path)
    assert sequence.n_stamps == 2
    np.testing.assert_array_equal(sequence.stamps[:, 0, 0], [1, 2])
    assert np.all(np.isnan(sequence.timestamps_mjd))


def test_offline_duplicate_sensor_files_are_rejected(tmp_path):
    paths = [
        write_sequence(tmp_path / name, "R00_SG0", [make_stamp()])
        for name in ["first.fits", "second.fits"]
    ]
    with pytest.raises(
        ValueError, match="Duplicate sensor 'R00_SG0'.*first.fits.*second.fits"
    ):
        run_offline(paths)


def test_reference_subset_update_preserves_omitted_sensors():
    runner = MultiSensorRunner(["a", "b"], GuiderTrackerConfig(seed_frames=5))
    seeds = np.stack([make_stamp(seed=index) for index in range(5)])
    assert runner.lock_references({"a": seeds, "b": seeds}) == ["a", "b"]
    reference_b = runner.references["b"]
    assert runner.lock_references({"a": np.zeros_like(seeds)}) == []
    assert runner.references == {"b": reference_b}
    measurements, combined = runner.process_acquisition(
        10, {"a": seeds[0], "b": seeds[0]}
    )
    assert set(measurements) == {"b"}
    assert (combined.n_valid, combined.n_total) == (1, 1)


def test_runner_config_mutation_affects_next_measurement_without_reset():
    config = GuiderTrackerConfig(seed_frames=5)
    runner = MultiSensorRunner(["a", "b"], config)
    seeds = np.stack([make_stamp(seed=index) for index in range(5)])
    runner.lock_references({"a": seeds, "b": seeds})
    references = runner.references
    _, before = runner.process_acquisition(0, {"a": seeds[0], "b": seeds[0]})
    assert before.n_valid == 2
    config.min_snr = float("inf")
    measurements, after = runner.process_acquisition(1, {"a": seeds[1], "b": seeds[1]})
    assert (after.n_valid, after.n_total) == (0, 2)
    assert all(m.state == MeasurementState.CONVERGED for m in measurements.values())
    assert all(
        runner.references[name] is reference for name, reference in references.items()
    )


def test_offline_initial_prefix_keeps_zero_point_when_later_frames_move(tmp_path):
    # Giving the full cube to the new explicit batch API would place the
    # median reference near the later position, eight pixels away.
    stamps = [make_stamp(seed=index) for index in range(5)]
    stamps += [make_stamp((8, 0), seed=index) for index in range(5, 20)]
    path = write_sequence(tmp_path / "moving.fits", "R04_SG0", stamps)
    results = run_offline([path], GuiderTrackerConfig(seed_frames=5))
    assert [result.stamp_index for result in results] == list(range(20))
    expected = [(0, 0)] * 5 + [(8, 0)] * 15
    np.testing.assert_allclose(
        [(r.combined_dx, r.combined_dy) for r in results], expected, atol=0.05
    )


def test_offline_retries_failed_prefix_and_replays_rejected_seeds(
    tmp_path, monkeypatch
):
    # Six blank images prevent locking with 10 or 11 seeds. The sixth
    # good image permits a lock at 12, followed by two displaced images.
    attempts = []
    build_reference_image = Detection.build_reference_image

    def record_prefix(self, stamps):
        attempts.append(len(stamps))
        return build_reference_image(self, stamps)

    monkeypatch.setattr(Detection, "build_reference_image", record_prefix)
    stamps = [np.zeros(SHAPE, dtype=np.int32)] * 6
    stamps += [make_stamp(seed=index) for index in range(6, 12)]
    stamps += [make_stamp((1, 0.4), seed=12), make_stamp((2, -0.5), seed=13)]
    path = write_sequence(tmp_path / "late.fits", "R04_SG0", stamps)
    results = run_offline([path], GuiderTrackerConfig(seed_frames=10))
    assert attempts == [10, 11, 12]
    assert [result.stamp_index for result in results] == list(range(14))
    for result in results[:6]:
        assert (result.n_valid, result.n_total) == (0, 1)
        assert result.measurements["R04_SG0"].state == MeasurementState.NOT_CONVERGED
        assert result.per_sensor_offset == {}
        assert np.isnan(result.combined_dx)
    assert all((result.n_valid, result.n_total) == (1, 1) for result in results[6:])
    np.testing.assert_allclose(
        [(result.combined_dx, result.combined_dy) for result in results[6:]],
        [(0, 0)] * 6 + [(1, -0.4), (2, 0.5)],
        atol=0.05,
    )


def test_offline_sensors_lock_at_different_indices_without_losing_replay(tmp_path):
    camera_offsets = [(0, 0)] * 12 + [(2, -1), (0.4, -0.65)]
    paths = []
    for sensor in ("R00_SG0", "R04_SG0"):
        local = [
            (-dy, -dx) if sensor == "R00_SG0" else (dx, -dy)
            for dx, dy in camera_offsets
        ]
        stamps = [make_stamp(offset, seed=index) for index, offset in enumerate(local)]
        if sensor == "R04_SG0":
            stamps[:6] = [np.zeros(SHAPE, dtype=np.int32)] * 6
        paths.append(write_sequence(tmp_path / f"{sensor}.fits", sensor, stamps))
    results = run_offline(paths, GuiderTrackerConfig(seed_frames=5))
    assert [result.stamp_index for result in results] == list(
        range(len(camera_offsets))
    )
    for index, result in enumerate(results):
        assert set(result.measurements) == {"R00_SG0", "R04_SG0"}
        assert (result.n_valid, result.n_total) == (1 if index < 6 else 2, 2)
        for offset in result.per_sensor_offset.values():
            np.testing.assert_allclose(offset, camera_offsets[index], atol=0.05)
    np.testing.assert_allclose(
        [(result.combined_dx, result.combined_dy) for result in results],
        camera_offsets,
        atol=0.05,
    )


@pytest.mark.parametrize("n_stamps", [3, 5])
def test_offline_waits_for_seed_threshold_even_at_end_of_input(tmp_path, n_stamps):
    path = write_sequence(
        tmp_path / "short.fits",
        "R00_SG0",
        [make_stamp(seed=index) for index in range(n_stamps)],
    )
    results = run_offline([path], GuiderTrackerConfig(seed_frames=5))
    if n_stamps < 5:
        assert results == []
        return
    assert [result.stamp_index for result in results] == list(range(n_stamps))
    assert all(result.n_valid == 1 for result in results)
    np.testing.assert_allclose(
        [(r.combined_dx, r.combined_dy) for r in results], 0, atol=0.05
    )


@pytest.mark.parametrize("n_failed", [3, 6])
def test_offline_shorter_failed_sensor_still_limits_acquisitions(tmp_path, n_failed):
    good = write_sequence(
        tmp_path / "good.fits",
        "R00_SG0",
        [make_stamp(seed=index) for index in range(7)],
    )
    failed = write_sequence(
        tmp_path / "failed.fits",
        "R04_SG0",
        [np.zeros(SHAPE, dtype=np.int32)] * n_failed,
    )
    results = run_offline([good, failed], GuiderTrackerConfig(seed_frames=5))
    if n_failed < 5:
        assert results == []
        return
    assert [result.stamp_index for result in results] == list(range(n_failed))
    assert all((result.n_valid, result.n_total) == (1, 1) for result in results)


def test_offline_does_not_lock_using_frames_beyond_shortest_sequence(tmp_path):
    good = write_sequence(
        tmp_path / "good.fits",
        "R00_SG0",
        [make_stamp(seed=index) for index in range(10)],
    )
    late_stamps = [np.zeros(SHAPE, dtype=np.int32)] * 6
    late_stamps += [make_stamp(seed=index) for index in range(6, 20)]
    late = write_sequence(tmp_path / "late.fits", "R04_SG0", late_stamps)
    config = GuiderTrackerConfig(seed_frames=5)
    assert len(run_offline([late], config)) == 20  # It can lock with more images.
    results = run_offline([good, late], config)
    assert [result.stamp_index for result in results] == list(range(10))
    assert all(set(result.measurements) == {"R00_SG0"} for result in results)
    assert all((result.n_valid, result.n_total) == (1, 1) for result in results)


@pytest.mark.parametrize("segment", [None, "unknown", "Segment", "Segment5"])
def test_offline_requires_segment_metadata(tmp_path, segment):
    path = write_sequence(
        tmp_path / "bad.fits", "R00_SG0", [make_stamp()], segment=segment
    )
    with pytest.raises(ValueError, match="Invalid ROISEG .* for sensor 'R00_SG0'"):
        run_offline([path])
