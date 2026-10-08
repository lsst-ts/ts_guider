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

import dataclasses
import logging
import math
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from lsst.ts.guider import GuiderCsc
from lsst.ts.guider.pipeline import (
    CentroidMeasurement,
    CombinedOffset,
    MeasurementState,
    OffsetCombiner,
)
from lsst.ts.guider.pipeline.centroid_measurement import FWHM_PER_SIGMA
from lsst.ts.xml.component_info import ComponentInfo
from lsst.ts.xml.enums.Guider import GuiderStatus


@pytest.fixture
def csc():
    """Exercise payload and queue methods without starting Kafka clients."""
    instance = object.__new__(GuiderCsc)
    instance.log = logging.getLogger("GuiderPayloadTest")
    instance._visit_sensors = set()
    instance._visit_rois = {"R04_SG0": (17, 30, 40), "R00_SG0": (5, 10, 20)}
    instance._roi_shape = (100, 140)
    instance._latest_identity = None
    instance._stamp_metadata_by_index = {}
    instance._last_stamp_index = None
    instance._visit_sequence = 3
    instance._series_metadata_published = False
    instance.processor = SimpleNamespace(_sensor_name=lambda index: f"R00_SG{index}")
    return instance


@pytest.fixture
def combined():
    # A known elliptical covariance, not the payload conversion formula.
    xx, yy, xy = 9.0, 4.0, 1.5
    measurement = CentroidMeasurement(
        x=70,
        y=50,
        snr=100,
        fwhm=FWHM_PER_SIGMA * (xx * yy - xy**2) ** 0.25,
        e1=(xx - yy) / (xx + yy),
        e2=2 * xy / (xx + yy),
        state=MeasurementState.PASSED_QUALITY,
    )
    rejected = dataclasses.replace(measurement, state=MeasurementState.CONVERGED)
    return CombinedOffset(
        stamp_index=65536,
        combined_dx=2.0,
        combined_dy=-3.0,
        n_valid=1,
        n_total=2,
        scatter_dx=math.nan,
        scatter_dy=math.nan,
        per_sensor_offset={"R00_SG0": (2.0, -3.0)},
        measurements={"R04_SG0": rejected, "R00_SG0": measurement},
    )


def test_payload_units_order_quality_and_unavailable_values(csc, combined):
    identity = dict(seqno=3, stamp=0, timestamp=1700000001.2, obsid="image")
    result = csc._build_per_guider_results_payload(combined, identity)
    assert result["sensor"] == "R00_SG0:R04_SG0"
    assert result["stamp"] == 0
    assert result["timestamp"] == identity["timestamp"]
    np.testing.assert_allclose(result["centroidDx"][:1], [0.02])
    np.testing.assert_allclose(result["centroidDy"][:1], [-0.03])
    assert np.isnan(result["centroidDx"][1:]).all()
    assert result["centroidFitQuality"][:2] == [1.0, 0.0]
    assert np.isnan(result["centroidFitQuality"][2:]).all()
    for key in (
        "flux",
        "fluxErr",
        "centroidX",
        "centroidY",
        "centroidXErr",
        "centroidYErr",
    ):
        assert len(result[key]) == 8
        assert np.isnan(result[key]).all()
    for key, value in (("momentXx", 9e-4), ("momentYy", 4e-4), ("momentXy", 1.5e-4)):
        np.testing.assert_allclose(result[key][:2], value)
        assert np.isnan(result[key][2:]).all()
    summary = csc._build_summary_results_payload(combined, identity)
    assert summary["deltaX"] == 0.02
    assert summary["deltaY"] == -0.03
    assert summary["goodStamps"] == 1
    assert np.isnan(summary["deltaRotation"])
    assert summary["qualityFlag"][:2] == [1.0, 0.0]
    series = csc._build_series_metadata_payload()
    assert series["sensor"] == result["sensor"]
    assert series["segment"] == [5, 17, 0, 0, 0, 0, 0, 0]
    assert series["startrow"][:2] == [10, 30]
    assert series["roiCommonNrows"] == 100
    assert series["roiCommonNcols"] == 140
    assert series["roiCommonIntegration"] == 0
    assert series["splitroi"] == [False] * 8


@pytest.mark.parametrize(
    "changes",
    [{"state": MeasurementState.NOT_CONVERGED}, {"e1": 1.0}, {"fwhm": math.nan}],
)
def test_unavailable_moments(csc, combined, changes):
    combined.measurements = {
        "R00_SG0": dataclasses.replace(combined.measurements["R00_SG0"], **changes)
    }
    result = csc._build_per_guider_results_payload(combined, {})
    for field in ("momentXx", "momentYy", "momentXy"):
        assert np.isnan(result[field]).all()


@pytest.mark.parametrize("state", list(MeasurementState))
def test_all_measurement_states(csc, combined, state):
    measurement = dataclasses.replace(combined.measurements["R00_SG0"], state=state)
    combined.measurements = {"R00_SG0": measurement}
    combined.per_sensor_offset = (
        {} if state != MeasurementState.PASSED_QUALITY else {"R00_SG0": (2, -3)}
    )
    combined.n_total = 1
    combined.n_valid = len(combined.per_sensor_offset)
    result = csc._build_per_guider_results_payload(combined, {})
    summary = csc._build_summary_results_payload(combined, {})
    expected = float(state == MeasurementState.PASSED_QUALITY)
    assert result["centroidFitQuality"][0] == summary["qualityFlag"][0] == expected
    assert np.isfinite(result["momentXx"][0]) == (
        state in (MeasurementState.CONVERGED, MeasurementState.PASSED_QUALITY)
    )
    assert np.isfinite(result["centroidDx"][0]) == bool(expected)


@pytest.mark.parametrize(
    "changes",
    [
        {"fwhm": 0},
        {"fwhm": -1},
        {"fwhm": math.inf},
        {"fwhm": 1e308},
        {"e1": math.nan},
        {"e1": math.inf},
        {"e2": math.inf},
        {"e1": 1e308},
    ],
)
def test_invalid_shape_never_publishes_moments(csc, combined, changes):
    combined.measurements = {
        "R00_SG0": dataclasses.replace(combined.measurements["R00_SG0"], **changes)
    }
    result = csc._build_per_guider_results_payload(combined, {})
    for field in ("momentXx", "momentYy", "momentXy"):
        assert np.isnan(result[field]).all()


@pytest.mark.parametrize("size", [0, 8, 9])
def test_sensor_capacity(csc, combined, size):
    measurement = combined.measurements["R00_SG0"]
    combined.measurements = {f"sensor_{i}": measurement for i in range(size)}
    csc._visit_rois = {f"sensor_{i}": (i, 10, 20) for i in range(size)}
    builders = (
        lambda: csc._build_series_metadata_payload(),
        lambda: csc._build_per_guider_results_payload(combined, {}),
        lambda: csc._build_summary_results_payload(combined, {}),
    )
    for build in builders:
        if size > 8:
            with pytest.raises(ValueError, match="eight"):
                build()
        else:
            payload = build()
            for value in payload.values():
                if isinstance(value, list):
                    assert len(value) == 8
            if "sensor" in payload:
                assert payload["sensor"] == ":".join(sorted(combined.measurements))


@pytest.mark.parametrize(
    "labels, expected",
    [
        ({}, ""),
        ({"series_id": "series"}, "series"),
        ({"obs_id": "image", "series_id": "series"}, "image"),
    ],
)
def test_missing_metadata_and_observation_label_fallback(csc, labels, expected):
    csc._record_stamp_metadata(
        np.zeros((10, 20)),
        SimpleNamespace(sensor_index=0, stamp_index=5, **labels),
    )
    assert csc._latest_identity == dict(seqno=3, stamp=5, timestamp=0.0, obsid=expected)


def test_first_sensor_supplies_identity_and_absent_measurement_is_not_rejected(
    csc, combined
):
    csc._enqueue = Mock()
    for sensor, label, timestamp in (
        (1, "first", 2_000_000_000),
        (0, "later", 3_000_000_000),
    ):
        csc._record_stamp_metadata(
            np.zeros((10, 20)),
            SimpleNamespace(
                sensor_index=sensor, stamp_index=0, timestamp_ns=timestamp, obs_id=label
            ),
        )
    csc._handle_combined_offset(combined)
    payloads = dict(call.args for call in csc._enqueue.call_args_list)
    assert payloads["evt_stateMetadata"]["sensors"] == "R00_SG0:R00_SG1"
    # Received sensors, ROI metadata, and returned measurements are distinct.
    assert payloads["evt_perGuiderResults"]["sensor"] == "R00_SG0:R04_SG0"
    assert payloads["evt_perGuiderResults"]["centroidFitQuality"][:2] == [1.0, 0.0]
    for topic in ("evt_stateMetadata", "evt_summaryResults", "evt_perGuiderResults"):
        assert payloads[topic]["obsid"] == "first"
        assert payloads[topic]["timestamp"] == 2.0


def test_sequence_rollover_stop_keeps_finishing_series_identity(csc):
    csc._enqueue = Mock()
    csc._reset_visit_bookkeeping(65535)
    for sensor in (1, 0):
        csc._record_stamp_metadata(
            np.zeros((10, 20)),
            SimpleNamespace(
                sensor_index=sensor,
                stamp_index=9,
                obs_id="finishing-image",
                timestamp_ns=5_000_000_000,
            ),
        )
    csc._reset_visit_bookkeeping(0)
    csc._record_stamp_metadata(
        np.zeros((10, 20)),
        SimpleNamespace(
            sensor_index=2,
            stamp_index=0,
            obs_id="new-image",
            timestamp_ns=6_000_000_000,
        ),
    )
    # Finalized offsets must not be emitted as fresh live corrections.
    csc._handle_visit_complete(65535, [object()])
    csc._handle_visit_start(0, "new-image")
    calls = csc._enqueue.call_args_list
    assert [call.args[0] for call in calls] == ["evt_stateMetadata"] * 2
    assert calls[0].args[1] == dict(
        status=GuiderStatus.STOP,
        seqno=65535,
        stamp=9,
        timestamp=5.0,
        obsid="finishing-image",
        sensors="R00_SG0:R00_SG1",
    )
    assert calls[1].args[1] == dict(
        status=GuiderStatus.START,
        seqno=0,
        stamp=0,
        timestamp=6.0,
        obsid="new-image",
        sensors="R00_SG2",
    )


def test_recording_ninth_sensor_fails(csc):
    for index in range(8):
        csc._record_stamp_metadata(
            np.zeros((10, 20)),
            SimpleNamespace(
                sensor_index=index,
                stamp_index=0,
            ),
        )
    with pytest.raises(ValueError, match="eight"):
        csc._record_stamp_metadata(
            np.zeros((10, 20)),
            SimpleNamespace(
                sensor_index=8,
                stamp_index=0,
            ),
        )


def test_raw_acquisition_identity_survives_rollover(csc, combined):
    csc.processor = SimpleNamespace(_sensor_name=lambda index: f"R00_SG{index}")
    # A buffered seed record with raw index 0 must not be reused after wrap.
    csc._stamp_metadata_by_index[0] = {"identity": {"obsid": "old"}, "sensors": set()}
    csc._last_stamp_index = 65535
    for sensor in (1, 0):
        csc._record_stamp_metadata(
            np.zeros((100, 140)),
            SimpleNamespace(
                sensor_index=sensor,
                stamp_index=0,
                timestamp_ns=1_700_000_001_000_000_000,
                obs_id="after-wrap",
                series_id="series",
            ),
        )
    csc._enqueue = Mock()
    csc._handle_combined_offset(combined)
    calls = dict(call.args for call in csc._enqueue.call_args_list)
    for topic in ("evt_stateMetadata", "evt_summaryResults", "evt_perGuiderResults"):
        assert calls[topic]["stamp"] == 0
        assert calls[topic]["seqno"] == 3
        assert calls[topic]["obsid"] == "after-wrap"
        assert calls[topic]["timestamp"] == 1_700_000_001.0
    assert calls["evt_stateMetadata"]["sensors"] == "R00_SG0:R00_SG1"
    assert calls["evt_seriesMetadata"]["force_output"] is True
    assert 0 not in csc._stamp_metadata_by_index


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
            # Covariance xx=1.2, yy=0.8, xy=-0.1; sigma is det(M)**(1/4).
            fwhm=(1.2 * 0.8 - 0.1**2) ** 0.25 * FWHM_PER_SIGMA,
            e1=0.2,
            e2=-0.1,
            state=(
                MeasurementState.PASSED_QUALITY
                if index < n_valid
                else MeasurementState.CONVERGED
            ),
        )
    # Deliberately reverse input order: event arrays must sort by sensor.
    return OffsetCombiner().combine(
        15,
        dict(reversed(list(measurements.items()))),
        {name: CentroidMeasurement(x=0, y=0) for name in measurements},
    )


@pytest.mark.parametrize("n_valid", [0, 1, 2])
def test_combined_results_and_diagnostic_logs(publisher, topic_info, caplog, n_valid):
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
        payload = {
            key: value for key, value in payload.items() if key != "force_output"
        }
        samples[name] = info.make_dataclass()(**payload)

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
