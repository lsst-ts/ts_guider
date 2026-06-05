# This file is part of ts_guider.
#
# Developed for Vera C. Rubin Observatory Telescope and Site Systems.
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Unit tests for the guiderGDS pybind bindings.

These exercise the C++/Python boundary in pybind_GDS.cpp without a
live DAQ or emulator, using the test-only _invoke_callback_for_test
seam to run a synthetic stamp through the real callback path.

The guiderGDS extension module is only available once cpp/cmake has
been built (which requires the daq-sdk). When it is not importable
(e.g. CI before the SDK is in the image) the whole module is skipped
rather than failed. To run locally, build the module and put it on
PYTHONPATH, e.g.::

    cd cpp/cmake
    cmake -S . -B build -DBUILD_TESTS=ON
    cmake --build build
    PYTHONPATH=build pytest ../../tests/test_guiderGDS_bindings.py
"""

import numpy as np
import pytest

guiderGDS = pytest.importorskip("guiderGDS")


def test_callback_receives_int32_array_and_metadata():
    sent = np.arange(12, dtype=np.int32).reshape(3, 4)
    received = {}

    def on_stamp(pixels, metadata):
        received["shape"] = pixels.shape
        received["dtype"] = pixels.dtype
        received["values"] = pixels.copy()
        received["sensor_index"] = metadata.sensor_index
        received["sequence"] = metadata.sequence
        received["stamp_index"] = metadata.stamp_index
        received["timestamp_ns"] = metadata.timestamp_ns

    guiderGDS._invoke_callback_for_test(
        on_stamp,
        sent,
        sensor_index=3,
        sequence=1,
        stamp_index=7,
        timestamp_ns=1779413890189701790,
    )

    assert received["shape"] == (3, 4)
    assert received["dtype"] == np.int32
    np.testing.assert_array_equal(received["values"], sent)
    assert received["sensor_index"] == 3
    assert received["sequence"] == 1
    assert received["stamp_index"] == 7
    assert received["timestamp_ns"] == 1779413890189701790


def test_callback_receives_independent_copy():
    # The array handed to Python must be a copy of the decode buffer,
    # not a view aliasing it; mutating the source afterwards must not
    # change what the callback received (see PythonStampCallback).
    sent = np.ones((2, 2), dtype=np.int32)
    holder = {}

    def on_stamp(pixels, metadata):
        holder["pixels"] = pixels

    guiderGDS._invoke_callback_for_test(on_stamp, sent)

    sent[:] = 99
    assert (holder["pixels"] == 1).all()


def test_callback_exception_is_contained():
    # A raising Python callback must not propagate out / crash the
    # process; PythonStampCallback::operator() catches and reports it.
    def on_stamp(pixels, metadata):
        raise ValueError("boom")

    guiderGDS._invoke_callback_for_test(on_stamp, np.zeros((2, 2), dtype=np.int32))


def test_location_set_any_is_truthy():
    assert bool(guiderGDS.LocationSet.any())


def test_location_set_empty_is_falsy():
    assert not bool(guiderGDS.LocationSet())


def test_location_set_from_string_constructs():
    # The daq-sdk location-string constructor must accept the format the
    # standard examples use. Its truthiness reflects SDK-internal match
    # state, not "non-empty string", so only assert construction here.
    assert isinstance(guiderGDS.LocationSet("R00"), guiderGDS.LocationSet)
