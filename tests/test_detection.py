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
from lsst.ts.guider.pipeline import GuiderTrackerConfig
from lsst.ts.guider.pipeline.detection import (
    build_reference_image,
    find_candidate_centroids,
    subtract_annulus_background,
)


def test_reference_uses_seed_window_and_suppresses_outlier():
    stamps = np.full((6, 20, 30), 100, dtype=np.int32)
    stamps[0, 10, 15] = 10_000
    stamps[-1] = 20_000
    original = stamps.copy()
    reference = build_reference_image(stamps, 5)
    assert reference.dtype == np.float32
    assert reference.shape == (20, 30)
    assert np.all((reference >= 100) & (reference < 101))
    np.testing.assert_array_equal(reference, build_reference_image(stamps, 5))
    np.testing.assert_array_equal(stamps, original)


def test_single_reference_is_an_independent_float_image():
    stamps = np.arange(12).reshape(1, 3, 4)
    reference = build_reference_image(stamps, 1)
    np.testing.assert_array_equal(reference, stamps[0])
    assert reference.dtype == np.float32
    reference[:] = 0
    assert stamps[0, -1, -1] == 11


@pytest.mark.parametrize("value", [0, np.nan, np.inf])
def test_no_candidates_in_unusable_image(value):
    assert (
        find_candidate_centroids(np.full((60, 100), value), GuiderTrackerConfig()) == []
    )


def test_detection_recovers_source_position():
    rows, columns = np.indices((60, 100))
    image = 100 + np.random.default_rng(0).normal(0, 2, (60, 100))
    image += 1_000 * np.exp(-((columns - 75) ** 2 + (rows - 30) ** 2) / 8)
    candidates = find_candidate_centroids(image, GuiderTrackerConfig())
    assert len(candidates) == 1
    np.testing.assert_allclose(candidates[0][:2], (75, 30), atol=0.05)


def test_annulus_background_excludes_source():
    image = np.full((50, 50), 100.0)
    image[25, 25] += 1_000
    subtracted, noise = subtract_annulus_background(image, 10, 20)
    assert subtracted[25, 25] == 1_000
    assert subtracted[0, 0] == 0
    assert noise == 0
    assert image[0, 0] == 100
