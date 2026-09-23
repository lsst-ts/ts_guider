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

import logging

import numpy as np
import pytest
from lsst.ts.guider.pipeline import Detection, GuiderTrackerConfig


@pytest.mark.parametrize("seed_frames", [1, 2, 10])
def test_reference_uses_all_supplied_seeds_and_suppresses_outlier(seed_frames):
    stamps = np.full((5, 20, 30), 100, dtype=np.int32)
    stamps[:2] = 0
    stamps[0, 10, 15] = 10_000
    original = stamps.copy()
    detection = Detection(GuiderTrackerConfig(seed_frames=seed_frames))
    reference = detection.build_reference_image(stamps)
    assert reference.dtype == np.float32
    assert reference.shape == (20, 30)
    assert np.all((reference >= 100) & (reference < 101))
    np.testing.assert_array_equal(reference, detection.build_reference_image(stamps))
    np.testing.assert_array_equal(stamps, original)


@pytest.mark.parametrize("seed_frames", [1, 10])
def test_single_reference_is_an_independent_float_image(seed_frames):
    stamps = np.arange(12).reshape(1, 3, 4)
    reference = Detection(
        GuiderTrackerConfig(seed_frames=seed_frames)
    ).build_reference_image(stamps)
    np.testing.assert_array_equal(reference, stamps[0])
    assert reference.dtype == np.float32
    reference[:] = 0
    assert stamps[0, -1, -1] == 11


@pytest.mark.parametrize("value", [0, np.nan, np.inf])
def test_no_candidates_in_unusable_image(value):
    assert (
        Detection(GuiderTrackerConfig()).find_candidate_centroids(
            np.full((60, 100), value)
        )
        == []
    )


def test_detection_recovers_source_position():
    rows, columns = np.indices((60, 100))
    image = 100 + np.random.default_rng(0).normal(0, 2, (60, 100))
    image += 1_000 * np.exp(-((columns - 75) ** 2 + (rows - 30) ** 2) / 8)
    candidates = Detection(GuiderTrackerConfig()).find_candidate_centroids(image)
    assert len(candidates) == 1
    np.testing.assert_allclose(candidates[0][:2], (75, 30), atol=0.05)


def test_annulus_background_excludes_source():
    image = np.full((50, 50), 100.0)
    image[25, 25] += 1_000
    subtracted, noise = Detection(GuiderTrackerConfig()).subtract_annulus_background(
        image, 10, 20
    )
    assert subtracted[25, 25] == 1_000
    assert subtracted[0, 0] == 0
    assert noise == 0
    assert image[0, 0] == 100


def test_reference_centroid_uses_brightest_detected_footprint():
    rows, columns = np.indices((100, 160))
    image = 100 + np.random.default_rng(0).normal(0, 2, rows.shape)
    image += 1_000 * np.exp(-((columns - 100) ** 2 + (rows - 50) ** 2) / 8)
    image += 300 * np.exp(-((columns - 40) ** 2 + (rows - 50) ** 2) / 8)
    detection = Detection(GuiderTrackerConfig())
    np.testing.assert_allclose(
        detection.find_reference_centroid(image), (100, 50), atol=0.05
    )
    assert detection.find_reference_centroid(np.zeros_like(image)) is None


def test_detection_uses_supplied_logger(caplog):
    logger = logging.getLogger("test.guider.detection")
    Detection(GuiderTrackerConfig(), log=logger).find_candidate_centroids(
        np.zeros((60, 100))
    )
    assert any(
        record.name == logger.name and "Noise estimate is zero" in record.message
        for record in caplog.records
    )


def test_empty_annulus_preserves_image_and_default_noise():
    image = np.ones((5, 5))
    subtracted, noise = Detection(GuiderTrackerConfig()).subtract_annulus_background(
        image, 10, 20
    )
    np.testing.assert_array_equal(subtracted, image)
    assert noise == 1
