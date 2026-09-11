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

__all__ = ["GuiderCsc", "run_guider_csc"]

import asyncio
import math

from lsst.ts import salobj
from lsst.ts.xml.enums.Guider import GuiderStatus

from . import __version__
from .config_schema import CONFIG_SCHEMA
from .pipeline import (
    CombinedOffset,
    GuiderTrackerConfig,
    StreamingGuiderProcessor,
)

NUMBER_OF_GUIDER_SENSORS = 8

# Converts pixel offsets to millimeters.
# guide sensors have 10 micron pixels.
PIXEL_SIZE_MM = 0.010

# FWHM = 2 * sqrt(2 * ln 2) * sigma for a Gaussian profile.
SIGMA_PER_FWHM = 1.0 / 2.3548200450309493


def padded_array(values, fill):
    """Pad a per-sensor sequence to ``NUMBER_OF_GUIDER_SENSORS``."""
    padded = list(values)[:NUMBER_OF_GUIDER_SENSORS]
    padded += [fill] * (NUMBER_OF_GUIDER_SENSORS - len(padded))
    return padded


class GuiderCsc(salobj.ConfigurableCsc):
    """Guider CSC.

    Runs the streaming guider pipeline
    (`lsst.ts.guider.pipeline.StreamingGuiderProcessor`) on the live
    DAQ/GDS stamp stream while ENABLED, publishing the Guider events
    and telemetry defined in ts_xml. The Guider interface has no
    guiding commands: guiding follows the DAQ stream, which starts
    and stops with the camera guider series.

    Parameters
    ----------
    config_dir : `str`, `pathlib.Path`, or `None`, optional
        Directory of configuration files, or `None` for the
        standard configuration directory (obtained from
        `get_config_pkg`).
    initial_state : `salobj.State` or `int`, optional
        The initial state of the CSC. This is provided
        for unit testing, as real CSCs should start up in
        `lsst.ts.salobj.State.STANDBY`, the default.
    simulation_mode : `int`, optional
        Simulation mode.
    index : `int` or `None`, optional
        SAL index; must be a valid Guider index
        (1=MainTel, 2=AuxTel).
    override : `str`, optional
        Configuration override file to apply if ``initial_state``
        is `salobj.State.DISABLED` or `salobj.State.ENABLED`.

    Raises
    ------
    salobj.ExpectedError
        If ``initial_state`` or ``simulation_mode``
        is invalid.

    Notes
    -----
    **Simulation Modes**

    Supported simulation modes:

    * 0: regular operation; subscribes a ``guiderGDS.DaqStampSource``
      to the configured partition.
    * 1: simulation mode; no DAQ connection. Stamps can be fed
      directly to `process_stamp` (used by the unit tests).

    **Threading**

    ``guiderGDS`` delivers stamps on a C++ worker thread and the
    pipeline callbacks fire synchronously on that thread. The
    callbacks therefore only build SAL payloads and hand them to the
    asyncio event loop through a thread-safe queue; `_publish_loop`
    drains the queue and writes the topics.
    """

    valid_simulation_modes = (0, 1)
    version = __version__
    # Allow "run_guider_csc 1 --state enabled" for manual runs.
    enable_cmdline_state = True

    def __init__(
        self,
        config_dir=None,
        initial_state=salobj.State.STANDBY,
        simulation_mode=0,
        index=None,
        override="",
    ):
        self.config = None
        self.processor = None
        self.stamp_source = None
        self._event_loop = None
        self._publish_queue = None
        self._publish_task = None

        # Per-visit bookkeeping recorded in process_stamp (worker
        # thread) and consumed by the pipeline callbacks, which fire
        # synchronously on the same thread.
        self._visit_sequence = None
        self._stamp_metadata_by_index = {}
        self._visit_rois = {}
        self._roi_shape = (0, 0)
        self._series_metadata_published = False
        self._latest_identity = None
        self._completed_visit_sensors = ""
        self._completed_visit_identity = None

        super().__init__(
            name="Guider",
            index=index,
            config_schema=CONFIG_SCHEMA,
            config_dir=config_dir,
            initial_state=initial_state,
            simulation_mode=simulation_mode,
            override=override,
        )

    @staticmethod
    def get_config_pkg():
        return "ts_config_ocs"

    async def configure(self, config):
        self.config = config

    async def handle_summary_state(self):
        if self.summary_state == salobj.State.ENABLED:
            await self.start_guiding()
        else:
            await self.stop_guiding()

    async def close_tasks(self):
        await self.stop_guiding()
        await super().close_tasks()

    async def start_guiding(self):
        """Start the streaming pipeline and, in regular operation,
        subscribe to the DAQ stamp stream.
        """
        if self.processor is not None:
            return

        self._event_loop = asyncio.get_running_loop()
        self._publish_queue = asyncio.Queue()
        self._publish_task = asyncio.create_task(self._publish_loop())

        self._visit_sequence = None
        self._stamp_metadata_by_index = {}
        self._visit_rois = {}
        self._roi_shape = (0, 0)
        self._series_metadata_published = False
        self._latest_identity = None
        self._completed_visit_sensors = ""
        self._completed_visit_identity = None

        default_tracker_config = GuiderTrackerConfig()
        tracker_config = GuiderTrackerConfig(
            seed_frames=getattr(
                self.config, "seed_frames", default_tracker_config.seed_frames
            ),
            min_snr=getattr(self.config, "min_snr", default_tracker_config.min_snr),
        )
        self.processor = StreamingGuiderProcessor(
            tracker_config,
            on_combined_offset=self._handle_combined_offset,
            on_visit_start=self._handle_visit_start,
            on_visit_complete=self._handle_visit_complete,
        )
        self.log.info(
            f"Guider pipeline ready.\n"
            f"seed_frames: {tracker_config.seed_frames}\n"
            f"min_snr: {tracker_config.min_snr}\n"
            f"simulation_mode: {self.simulation_mode}"
        )

        if self.simulation_mode != 0:
            return

        try:
            import guiderGDS

            self.stamp_source = guiderGDS.DaqStampSource(
                partition=self.config.partition,
                locations=guiderGDS.LocationSet.any(),
            )
            self.stamp_source.start_stamp_stream(self.process_stamp)
        except Exception as e:
            self.log.exception("Failed to subscribe to the DAQ stamp stream.")
            await self.stop_guiding()
            raise salobj.ExpectedError(
                f"Failed to subscribe to the DAQ stamp stream: {e}"
            )
        self.log.info(
            f"Subscribed to guider stamps on partition " f"'{self.config.partition}'."
        )

    async def stop_guiding(self):
        """Stop the DAQ stream, finalize the pipeline and flush the
        pending SAL payloads.
        """
        if self.processor is None and self.stamp_source is None:
            return

        stamp_source = self.stamp_source
        self.stamp_source = None
        if stamp_source is not None:
            # unsubscribe blocks until the C++ worker thread joins.
            await asyncio.get_running_loop().run_in_executor(
                None, stamp_source.stop_stamp_stream
            )

        processor = self.processor
        self.processor = None
        if processor is not None:
            self._completed_visit_sensors = ":".join(sorted(self._visit_rois))
            self._completed_visit_identity = self._latest_identity
            try:
                processor.finalize()
            except Exception:
                self.log.exception(
                    "Failed to finalize the guider pipeline; continuing ..."
                )

        if self._publish_queue is not None:
            # Yield once so enqueues scheduled with
            # call_soon_threadsafe reach the queue ahead of the
            # sentinel that stops the publish loop.
            await asyncio.sleep(0)
            self._publish_queue.put_nowait(None)
            await self._publish_task
            self._publish_queue = None
            self._publish_task = None

    def process_stamp(self, pixels, metadata):
        """Handle one guider stamp.

        Called on the DAQ C++ worker thread. Records the per-stamp
        identity (sequence, timestamp, obsid) and ROI geometry the
        SAL topics need: the pipeline consumes the DAQ metadata for
        visit boundaries and sensor orientation and produces only
        science results keyed by ``stamp_index``. The stamp is then
        handed to the streaming processor, whose callbacks enqueue
        the SAL payloads.
        """
        processor = self.processor
        if processor is None:
            return
        sequence = int(getattr(metadata, "sequence", 0))
        if self._visit_sequence is None or sequence != self._visit_sequence:
            self._reset_visit_bookkeeping(sequence)
        self._record_stamp_metadata(pixels, metadata)
        processor.on_stamp(pixels, metadata)

    def _reset_visit_bookkeeping(self, sequence):
        """Start bookkeeping for a new visit (DAQ sequence).

        Called at the first stamp of the new visit, before the
        processor ends the previous one, so the finishing visit's
        sensors and identity are snapshotted here for the STOP state
        event published by `_handle_visit_complete`.
        """
        self._completed_visit_sensors = ":".join(sorted(self._visit_rois))
        self._completed_visit_identity = self._latest_identity
        self._visit_sequence = sequence
        self._stamp_metadata_by_index = {}
        self._visit_rois = {}
        self._series_metadata_published = False

    def _record_stamp_metadata(self, pixels, metadata):
        """Record one stamp's identity and ROI for later publication."""
        sensor_name = self.processor._sensor_name(int(metadata.sensor_index))
        stamp_index = int(metadata.stamp_index)
        identity = dict(
            seqno=self._visit_sequence,
            stamp=stamp_index,
            timestamp=float(getattr(metadata, "timestamp_ns", 0)) * 1e-9,
            obsid=str(
                getattr(metadata, "obs_id", "") or getattr(metadata, "series_id", "")
            ),
        )
        record = self._stamp_metadata_by_index.setdefault(
            stamp_index, dict(identity=identity, sensors=set())
        )
        record["sensors"].add(sensor_name)
        self._latest_identity = identity

        shape = getattr(pixels, "shape", None)
        if shape is not None and len(shape) == 2:
            self._roi_shape = (int(shape[0]), int(shape[1]))
        segment = getattr(metadata, "segment", None)
        startrow = getattr(metadata, "startrow", None)
        startcol = getattr(metadata, "startcol", None)
        if (
            sensor_name not in self._visit_rois
            and segment is not None
            and startrow is not None
            and startcol is not None
        ):
            self._visit_rois[sensor_name] = (
                int(segment),
                int(startrow),
                int(startcol),
            )

    def _default_identity(self):
        """Latest recorded stamp identity, or zeros before any stamp."""
        return self._latest_identity or dict(seqno=0, stamp=0, timestamp=0.0, obsid="")

    def _enqueue(self, topic_attribute_name, payload):
        """Queue one SAL payload for publication on the event loop.

        Safe to call from the DAQ worker thread.
        """
        queue = self._publish_queue
        event_loop = self._event_loop
        if queue is None or event_loop is None:
            return
        event_loop.call_soon_threadsafe(
            queue.put_nowait, (topic_attribute_name, payload)
        )

    async def _publish_loop(self):
        """Drain the publish queue, writing one topic per item."""
        while True:
            item = await self._publish_queue.get()
            if item is None:
                return
            topic_attribute_name, payload = item
            try:
                topic = getattr(self, topic_attribute_name)
                await topic.set_write(**payload)
            except Exception:
                self.log.exception(
                    f"Failed to write {topic_attribute_name}; continuing ..."
                )

    def _handle_visit_start(self, sequence, label):
        """Publish the START state event at the first stamp of a visit.

        Pipeline callback; runs on the DAQ worker thread.
        """
        identity = self._default_identity()
        self._enqueue(
            "evt_stateMetadata",
            dict(
                status=GuiderStatus.START,
                sensors=":".join(sorted(self._visit_rois)),
                **identity,
            ),
        )

    def _handle_visit_complete(self, sequence, offsets):
        """Publish the STOP state event when a visit ends.

        Pipeline callback; runs on the DAQ worker thread (sequence
        boundary) or on the event loop (finalize at stop_guiding).
        The finishing visit's sensors and identity were snapshotted
        by `_reset_visit_bookkeeping` or `stop_guiding`.
        """
        identity = self._completed_visit_identity or self._default_identity()
        self._enqueue(
            "evt_stateMetadata",
            dict(
                status=GuiderStatus.STOP,
                sensors=self._completed_visit_sensors,
                **identity,
            ),
        )

    def _handle_combined_offset(self, combined: CombinedOffset):
        """Publish the topics for one live combined acquisition.

        Pipeline callback; runs on the DAQ worker thread. Publishes
        seriesMetadata once per visit (at the first combine, when the
        ROI of every locked sensor has been seen), then the STAMP
        state event, the offsets telemetry, and the summary and
        per-guider results for the acquisition.
        """
        record = self._stamp_metadata_by_index.pop(combined.stamp_index, None)
        if record is not None:
            identity = record["identity"]
            sensors = ":".join(sorted(record["sensors"]))
        else:
            identity = dict(self._default_identity(), stamp=combined.stamp_index)
            sensors = ":".join(sorted(combined.measurements))

        if not self._series_metadata_published:
            self._series_metadata_published = True
            self._enqueue("evt_seriesMetadata", self._build_series_metadata_payload())

        self._enqueue(
            "evt_stateMetadata",
            dict(status=GuiderStatus.STAMP, sensors=sensors, **identity),
        )
        self._enqueue(
            "tel_offsets",
            dict(
                x=combined.combined_dx * PIXEL_SIZE_MM,
                y=combined.combined_dy * PIXEL_SIZE_MM,
                x_err=combined.error_dx * PIXEL_SIZE_MM,
                y_err=combined.error_dy * PIXEL_SIZE_MM,
                # The pipeline does not measure a rotation offset yet.
                rotation=math.nan,
                n_sensors=combined.n_valid,
            ),
        )
        self._enqueue(
            "evt_summaryResults",
            self._build_summary_results_payload(combined, identity),
        )
        self._enqueue(
            "evt_perGuiderResults",
            self._build_per_guider_results_payload(combined, identity),
        )

    def _build_series_metadata_payload(self):
        sensor_names = sorted(self._visit_rois)
        rois = [self._visit_rois[name] for name in sensor_names]
        return dict(
            roi_common_nrows=self._roi_shape[0],
            roi_common_ncols=self._roi_shape[1],
            # The integration time is not carried by the stamp
            # stream; it is part of the DAQ series configuration.
            roi_common_integration=0,
            sensor=":".join(sensor_names),
            segment=padded_array([roi[0] for roi in rois], 0),
            startrow=padded_array([roi[1] for roi in rois], 0),
            startcol=padded_array([roi[2] for roi in rois], 0),
            # Split-ROI stamps are not flagged by the stamp stream.
            splitroi=padded_array([], False),
        )

    def _build_summary_results_payload(self, combined, identity):
        # quality_flag is per sensor, in the same sorted-sensor order
        # as the perGuiderResults arrays: 1 passed the quality cuts,
        # 0 failed.
        quality_flags = [
            1.0 if combined.measurements[name].passed_quality else 0.0
            for name in sorted(combined.measurements)
        ]
        return dict(
            delta_x=combined.combined_dx * PIXEL_SIZE_MM,
            delta_y=combined.combined_dy * PIXEL_SIZE_MM,
            delta_x_err=combined.error_dx * PIXEL_SIZE_MM,
            delta_y_err=combined.error_dy * PIXEL_SIZE_MM,
            scatter_x=combined.scatter_dx * PIXEL_SIZE_MM,
            scatter_y=combined.scatter_dy * PIXEL_SIZE_MM,
            # The pipeline does not measure a rotation offset yet.
            delta_rotation=math.nan,
            good_stamps=combined.n_valid,
            quality_flag=padded_array(quality_flags, math.nan),
            **identity,
        )

    def _build_per_guider_results_payload(self, combined, identity):
        sensor_names = sorted(combined.measurements)
        measurements = [combined.measurements[name] for name in sensor_names]

        centroid_dx = []
        centroid_dy = []
        for name in sensor_names:
            offset = combined.per_sensor_offset.get(name)
            if offset is None:
                centroid_dx.append(math.nan)
                centroid_dy.append(math.nan)
            else:
                centroid_dx.append(offset[0] * PIXEL_SIZE_MM)
                centroid_dy.append(offset[1] * PIXEL_SIZE_MM)

        # Second moments of a Gaussian from the HSM FWHM and
        # distortion: Ixx + Iyy = 2 sigma^2, Ixx - Iyy = e1 (Ixx + Iyy)
        # and 2 Ixy = e2 (Ixx + Iyy). Note these are in the amplifier
        # (ROI) frame, not rotated to DVCS.
        moment_xx = []
        moment_yy = []
        moment_xy = []
        for measurement in measurements:
            variance = (measurement.fwhm * SIGMA_PER_FWHM * PIXEL_SIZE_MM) ** 2
            moment_xx.append(variance * (1.0 + measurement.e1))
            moment_yy.append(variance * (1.0 - measurement.e1))
            moment_xy.append(variance * measurement.e2)

        nan_array = padded_array([], math.nan)
        return dict(
            sensor=":".join(sensor_names),
            # The HSM tracking path does not measure aperture flux.
            flux=nan_array,
            flux_err=nan_array,
            # Absolute DVCS centroid positions need the focal-plane
            # transform of the camera model (ROI origin + amplifier
            # to focal plane); not implemented yet. The guiding
            # quantity, the offset from the reference, is published
            # in centroid_dx/centroid_dy in camera-frame mm.
            centroid_x=nan_array,
            centroid_y=nan_array,
            centroid_x_err=nan_array,
            centroid_y_err=nan_array,
            centroid_dx=padded_array(centroid_dx, math.nan),
            centroid_dy=padded_array(centroid_dy, math.nan),
            moment_xx=padded_array(moment_xx, math.nan),
            moment_yy=padded_array(moment_yy, math.nan),
            moment_xy=padded_array(moment_xy, math.nan),
            centroid_fit_quality=padded_array(
                [
                    1.0 if measurement.passed_quality else 0.0
                    for measurement in measurements
                ],
                math.nan,
            ),
            **identity,
        )


def run_guider_csc() -> None:
    """Run the Guider CSC."""
    asyncio.run(GuiderCsc.amain(index=True))
