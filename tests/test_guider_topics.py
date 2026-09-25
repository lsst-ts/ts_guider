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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

import importlib.util
import logging
from pathlib import Path

import numpy as np
import pytest
from lsst.ts import salobj
from lsst.ts.guider.guider_csc import SIGMA_PER_FWHM, GuiderCsc
from lsst.ts.guider.pipeline import CentroidMeasurement, OffsetCombiner
from lsst.ts.xml.component_info import ComponentInfo

IDENTITY = dict(seqno=7, stamp=15, timestamp=1700000015.125, obsid="TEST_OBS_007")


@pytest.fixture
def topic_info(monkeypatch):
    """Read real XML topic definitions without starting a Kafka client."""
    monkeypatch.setenv("LSST_SITE", "test")
    return ComponentInfo(name="Guider", topic_subname="test_guider_payload").topics


@pytest.fixture
def publisher():
    """Exercise actual CSC payload methods without its network lifecycle."""
    csc = object.__new__(GuiderCsc)
    csc.log = logging.getLogger("test_guider_payload")
    csc._stamp_metadata_by_index = {
        15: dict(identity=IDENTITY.copy(), sensors={"R00_SG0", "R00_SG1", "R04_SG0"})
    }
    csc._latest_identity = dict(IDENTITY, stamp=16, timestamp=1700000016.125)
    csc._series_metadata_published = False
    csc._roi_shape = (100, 160)
    csc._visit_rois = {"R00_SG1": (1, 30, 40), "R00_SG0": (0, 10, 20)}
    queued = []
    csc._enqueue = lambda name, payload: queued.append((name, payload))
    return csc, queued


def make_combined(n_valid):
    """Use known pixel offsets to exercise real error/scatter calculations."""
    measurements = {}
    for index, name in enumerate(("R00_SG0", "R00_SG1", "R04_SG0")):
        measurements[name] = CentroidMeasurement(
            x=2 * (index + 1),
            y=4 * (index + 1),
            fwhm=1 / SIGMA_PER_FWHM,
            e1=0.2,
            e2=-0.1,
            converged=True,
            passed_quality=index < n_valid,
        )
    # Deliberately reverse input order: event arrays must sort by sensor.
    return OffsetCombiner().combine(
        15,
        dict(reversed(list(measurements.items()))),
        {name: (0, 0) for name in measurements},
    )


@pytest.mark.parametrize("n_valid", [0, 1, 2])
def test_current_xml_payloads_and_preserved_logs(
    publisher, topic_info, caplog, n_valid
):
    csc, queued = publisher
    combined = make_combined(n_valid)
    with caplog.at_level(logging.INFO, logger=csc.log.name):
        csc._handle_combined_offset(combined)
    assert [name for name, payload in queued] == [
        "evt_seriesMetadata",
        "evt_stateMetadata",
        "tel_offsets",
        "evt_summaryResults",
        "evt_perGuiderResults",
    ]
    samples = {}
    for name, payload in queued:
        info = topic_info[name]
        public_fields = {
            field
            for field in info.fields
            if not field.startswith("private_") and field != "salIndex"
        }
        assert set(payload) == public_fields
        # The generated dataclass rejects obsolete/unknown keyword fields.
        samples[name] = info.make_dataclass()(**payload)
        for field, length in info.array_fields.items():
            assert len(payload[field]) == length

    series = samples["evt_seriesMetadata"]
    assert (
        series.roiCommonNrows,
        series.roiCommonNcols,
        series.roiCommonIntegration,
    ) == (
        100,
        160,
        0,
    )
    assert series.sensor == "R00_SG0:R00_SG1"
    assert series.segment == [0, 1, 0, 0, 0, 0, 0, 0]
    assert series.startrow == [10, 30, 0, 0, 0, 0, 0, 0]
    assert series.startcol == [20, 40, 0, 0, 0, 0, 0, 0]
    assert series.splitroi == [False] * 8

    summary = samples["evt_summaryResults"]
    per_sensor = samples["evt_perGuiderResults"]
    for sample in (summary, per_sensor, samples["evt_stateMetadata"]):
        for field, value in IDENTITY.items():
            assert getattr(sample, field) == value
    expected_xy = [(np.nan, np.nan), (0.02, 0.04), (0.03, 0.06)][n_valid]
    offsets = samples["tel_offsets"]
    np.testing.assert_allclose((offsets.x, offsets.y), expected_xy, equal_nan=True)
    np.testing.assert_allclose(
        (summary.deltaX, summary.deltaY), expected_xy, equal_nan=True
    )
    assert np.isnan(offsets.rotation)
    assert np.isnan(summary.deltaRotation)
    assert summary.goodStamps == n_valid
    expected_quality = [float(index < n_valid) for index in range(3)] + [np.nan] * 5
    np.testing.assert_allclose(summary.qualityFlag, expected_quality, equal_nan=True)
    assert per_sensor.sensor == "R00_SG0:R00_SG1:R04_SG0"
    np.testing.assert_allclose(
        per_sensor.centroidFitQuality, expected_quality, equal_nan=True
    )
    for field, step in [("centroidDx", 0.02), ("centroidDy", 0.04)]:
        expected = [
            step * (index + 1) if index < n_valid else np.nan for index in range(3)
        ]
        np.testing.assert_allclose(getattr(per_sensor, field), expected + [np.nan] * 5)
    for field in (
        "flux",
        "fluxErr",
        "centroidX",
        "centroidY",
        "centroidXErr",
        "centroidYErr",
    ):
        assert np.isnan(getattr(per_sensor, field)).all()
    for field, value in [
        ("momentXx", 0.00012),
        ("momentYy", 0.00008),
        ("momentXy", -0.00001),
    ]:
        np.testing.assert_allclose(
            getattr(per_sensor, field), [value] * 3 + [np.nan] * 5
        )

    records = [record for record in caplog.records if record.name == csc.log.name]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert (
        "seqno=7 stamp=15 obsid='TEST_OBS_007' timestamp=1700000015.125000000"
        in message
    )
    assert f"valid_sensors={n_valid} total_sensors=3" in message
    for field, expected in zip(("x_mm", "y_mm"), expected_xy):
        value = float(message.split(field + "=")[1].split()[0])
        np.testing.assert_allclose(value, expected, equal_nan=True)
    for field, expected in [
        ("x_standard_error_mm", 0.01),
        ("y_standard_error_mm", 0.02),
        ("x_scatter_mm", np.sqrt(2) * 0.01),
        ("y_scatter_mm", np.sqrt(2) * 0.02),
    ]:
        assert message.count(field + "=") == 1
        value = float(message.split(field + "=")[1].split()[0])
        np.testing.assert_allclose(
            value, expected if n_valid == 2 else np.nan, equal_nan=True
        )


def test_diagnostics_for_every_live_acquisition(publisher, caplog):
    csc, queued = publisher
    combined = make_combined(2)
    with caplog.at_level(logging.INFO, logger=csc.log.name):
        for index in range(15, 20):
            combined.stamp_index = index
            csc._stamp_metadata_by_index[index] = dict(
                identity=dict(IDENTITY, stamp=index), sensors=set(combined.measurements)
            )
            csc._handle_combined_offset(combined)
    assert sum(name == "evt_seriesMetadata" for name, payload in queued) == 1
    records = [record for record in caplog.records if record.name == csc.log.name]
    assert len(records) == 5
    for index, record in enumerate(records, start=15):
        assert f"stamp={index} " in record.getMessage()


@pytest.mark.asyncio
async def test_watcher_reads_current_xml_samples(publisher, topic_info, capsys, caplog):
    path = Path(__file__).parents[1] / "scripts" / "watch_guider_topics.py"
    spec = importlib.util.spec_from_file_location("watch_guider_topics", path)
    watcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watcher)
    csc, queued = publisher
    with caplog.at_level(logging.INFO, logger=csc.log.name):
        csc._handle_combined_offset(make_combined(2))
    callbacks = {
        "evt_seriesMetadata": watcher.show_series_metadata,
        "evt_stateMetadata": watcher.show_state_metadata,
        "tel_offsets": watcher.show_offsets,
        "evt_summaryResults": watcher.show_summary_results,
        "evt_perGuiderResults": watcher.show_per_guider_results,
    }
    for name, payload in queued:
        await callbacks[name](topic_info[name].make_dataclass()(**payload))
    message = next(
        record.getMessage() for record in caplog.records if record.name == csc.log.name
    )
    await watcher.show_log_message(
        topic_info["evt_logMessage"].make_dataclass()(
            level=logging.INFO,
            message=message,
            traceback="",
        )
    )
    await watcher.show_summary_state(
        topic_info["evt_summaryState"].make_dataclass()(
            summaryState=salobj.State.ENABLED,
        )
    )
    await watcher.show_error_code(
        topic_info["evt_errorCode"].make_dataclass()(
            errorCode=42,
            errorReport="test error",
        )
    )
    output = capsys.readouterr().out
    assert "roi=100x160" in output
    assert "dx=+30.00 um dy=+60.00 um goodStamps=2" in output
    assert "x=+30.00 um y=+60.00 um" in output
    assert "R00_SG0:(+20.00,+40.00)um" in output
    assert "log[INFO] : " + message in output
    assert "summaryState     : ENABLED" in output
    assert "errorCode        : 42 test error" in output
