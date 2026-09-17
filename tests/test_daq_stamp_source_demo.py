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

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from lsst.ts.guider.demo import run_daq_stamp_source_demo


@pytest.fixture
def daq_source(monkeypatch):
    """Supply a controllable DAQ source without connecting to a partition."""
    source = Mock()
    locations = object()
    binding = SimpleNamespace(
        DaqStampSource=Mock(return_value=source),
        LocationSet=SimpleNamespace(any=Mock(return_value=locations)),
    )
    monkeypatch.setitem(sys.modules, "guiderGDS", binding)
    return binding, source, locations


def test_import_and_help_without_daq(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "guiderGDS", None)
    module = importlib.import_module("lsst.ts.guider.demo.run_daq_stamp_source_demo")
    importlib.reload(module)
    with pytest.raises(SystemExit) as exc:
        run_daq_stamp_source_demo(["--help"])
    assert exc.value.code == 0
    assert "--partition" in capsys.readouterr().out


def test_receive_requested_stamps(daq_source, capsys):
    binding, source, locations = daq_source
    metadata = SimpleNamespace(
        sensor_index=3, sensor_name="R00_SG0", segment="Segment10"
    )

    def deliver(callback):
        for _ in range(2):
            callback(np.arange(6, dtype=np.int32).reshape(2, 3), metadata)

    source.start_stamp_stream.side_effect = deliver
    run_daq_stamp_source_demo(
        ["--partition", "test", "--max-stamps", "2", "--timeout", "0.1"]
    )
    binding.DaqStampSource.assert_called_once_with(
        partition="test", locations=locations
    )
    source.stop_stamp_stream.assert_called_once_with()
    output = capsys.readouterr().out
    assert output.count("pixels.shape: (2, 3)") == 2
    assert "sensor_name: R00_SG0" in output
    assert "received 2 stamps in Python" in output
    assert "timeout" not in output


def test_timeout_stops_stream(daq_source, capsys):
    _, source, _ = daq_source
    run_daq_stamp_source_demo(["--timeout", "0.001"])
    source.stop_stamp_stream.assert_called_once_with()
    assert "timeout — only received 0 stamps" in capsys.readouterr().out


def test_interruption_stops_stream(daq_source, monkeypatch):
    _, source, _ = daq_source
    module = importlib.import_module("lsst.ts.guider.demo.run_daq_stamp_source_demo")
    done = Mock()
    done.wait.side_effect = KeyboardInterrupt
    monkeypatch.setattr(module.threading, "Event", Mock(return_value=done))
    with pytest.raises(KeyboardInterrupt):
        run_daq_stamp_source_demo([])
    source.stop_stamp_stream.assert_called_once_with()


@pytest.mark.parametrize(
    "args", [["--max-stamps", "0"], ["--timeout", "-1"], ["--timeout", "nan"]]
)
def test_invalid_arguments_do_not_start_stream(daq_source, args):
    binding, _, _ = daq_source
    with pytest.raises(SystemExit) as exc:
        run_daq_stamp_source_demo(args)
    assert exc.value.code == 2
    binding.DaqStampSource.assert_not_called()
