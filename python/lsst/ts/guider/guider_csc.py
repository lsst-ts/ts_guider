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

__all__ = ["GuiderCsc", "run_guider_csc"]

import asyncio
import math
import threading

from lsst.ts import salobj
from lsst.ts.xml.enums.Guider import GuiderStatus

from . import __version__
from .config_schema import CONFIG_SCHEMA
from .error_code import ErrorCode
from .pipeline.centroid_measurement import FWHM_PER_SIGMA
from .pipeline.combined_offset import CombinedOffset
from .pipeline.guider_tracker_config import GuiderTrackerConfig
from .pipeline.measurement_state import MeasurementState
from .pipeline.streaming_guider_processor import StreamingGuiderProcessor

NUMBER_OF_GUIDER_SENSORS = 8
SOURCE_STOP_TIMEOUT = 10.0
PUBLISH_DRAIN_TIMEOUT = 10.0

# Converts pixel offsets to millimeters.
# guide sensors have 10 micron pixels.
PIXEL_SIZE_MM = 0.010


def padded_array(values, fill):
    """Pad a per-sensor sequence to the XML array length.

    Parameters
    ----------
    values : iterable
        Up to eight values in sensor order.
    fill : object
        Value for unused array entries.

    Returns
    -------
    padded : `list`
        Exactly eight values.

    Raises
    ------
    ValueError
        If more than eight values are supplied.
    """
    padded = list(values)
    if len(padded) > NUMBER_OF_GUIDER_SENSORS:
        raise ValueError("Guider topics support at most eight sensors.")
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
    callbacks build SAL payloads and hand them to the asyncio event loop
    through a thread-safe queue; `_publish_loop` drains the queue and writes
    the topics. Combined diagnostics use the CSC logger, whose salobj handler
    also delivers worker-thread records to the event loop.
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
        self._lifecycle_lock = asyncio.Lock()
        self._stop_guiding_task = None
        self._run_error = None
        self._run_error_code = None
        self._run_failure_reported = False
        self._error_lock = threading.Lock()
        self._source_stop_failed = False
        self._source_stop_future = None
        self._processing_fault_task = None
        self._accept_stamps = False
        self._accept_publication = False
        self._closing = False

        # Per-visit bookkeeping recorded in process_stamp (worker
        # thread) and consumed by the pipeline callbacks, which fire
        # synchronously on the same thread.
        self._visit_sequence = None
        self._last_stamp_index = None
        self._stamp_metadata_by_index = {}
        self._visit_sensors = set()
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
        """Return the name of the package supplying CSC configuration.

        Returns
        -------
        name : `str`
            Configuration package name.
        """
        return "ts_config_ocs"

    async def configure(self, config):
        """Save the validated configuration for the next enabled run.

        Parameters
        ----------
        config : `types.SimpleNamespace`
            DAQ partition and optional tracker settings.
        """
        self.config = config

    async def handle_summary_state(self):
        """Run the pipeline only while enabled; fault on cleanup failures."""
        if self.summary_state == salobj.State.ENABLED:
            await self.start_guiding()
        else:
            try:
                await self.stop_guiding()
            except Exception:
                if self.summary_state == salobj.State.FAULT or self._closing:
                    self._run_failure_reported = True
                    self.log.exception("Guider cleanup retained a failed run.")
                elif not self._run_failure_reported:
                    self._run_failure_reported = True
                    await self.fault(
                        code=self._run_error_code,
                        report=f"Guider run failed: {self._run_error}",
                    )
                    raise
                # Once reported, the old failure must not prevent the
                # FAULT -> STANDBY recovery path. Startup still checks that
                # the previous source and publisher have actually stopped.

    async def close_tasks(self):
        """Stop acquisition and publication before closing salobj tasks."""
        self._closing = True
        try:
            await self.stop_guiding()
        finally:
            task = self._processing_fault_task
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await super().close_tasks()

    async def start_guiding(self):
        """Start processing and subscribe to DAQ in regular operation.

        Simulation mode accepts stamps through `process_stamp` instead.
        Repeated calls during a healthy run do nothing. A failed run may
        be replaced only after its source and publisher have stopped.

        Raises
        ------
        salobj.ExpectedError
            If startup fails or an earlier source shutdown is unconfirmed.
            The original startup exception is retained as the cause.
        """
        try:
            async with self._lifecycle_lock:
                await self._start_guiding()
        except (Exception, asyncio.CancelledError) as error:
            self._record_run_error(error, ErrorCode.DAQ_SUBSCRIPTION)
            try:
                await self.stop_guiding()
            except (Exception, asyncio.CancelledError) as cleanup_error:
                if cleanup_error is not self._run_error:
                    self.log.exception("Additional failure during startup cleanup.")
            if isinstance(error, asyncio.CancelledError):
                raise
            reason = str(self._run_error)
            if error is not self._run_error:
                reason += f"; {error}"
            report = (
                f"Failed to start Guider on partition {self.config.partition!r}: "
                f"{reason}."
            )
            if self._run_error_code == ErrorCode.DAQ_SUBSCRIPTION:
                report += (
                    " Check the guiderGDS binding, DAQ SDK runtime libraries, "
                    "and partition service."
                )
            # handle_summary_state runs after salobj changes its state;
            # raising alone would leave the CSC ENABLED without a source.
            await self.fault(code=self._run_error_code, report=report)
            raise salobj.ExpectedError(report) from error

    async def _start_guiding(self):
        """Create one run while holding the lifecycle lock."""
        if self._closing:
            raise salobj.ExpectedError("The Guider CSC is closing.")
        if self._source_stop_failed:
            raise salobj.ExpectedError(
                "DAQ worker termination is unconfirmed; restart the CSC process."
            )
        if self.processor is not None:
            return
        if self._publish_task is not None and not self._publish_task.done():
            raise salobj.ExpectedError("The previous publisher has not stopped.")
        if (
            self._processing_fault_task is not None
            and not self._processing_fault_task.done()
        ):
            raise salobj.ExpectedError(
                "The previous worker fault is still being handled."
            )

        self._stop_guiding_task = None
        self._run_error = None
        self._run_error_code = None
        self._run_failure_reported = False
        self._processing_fault_task = None
        self._event_loop = asyncio.get_running_loop()
        self._publish_queue = asyncio.Queue()
        self._accept_publication = True
        self._publish_task = asyncio.create_task(self._publish_loop())
        self._visit_sequence = None
        self._last_stamp_index = None
        self._stamp_metadata_by_index = {}
        self._visit_sensors = set()
        self._visit_rois = {}
        self._roi_shape = (0, 0)
        self._series_metadata_published = False
        self._latest_identity = None
        self._completed_visit_sensors = ""
        self._completed_visit_identity = None

        tracker_config = GuiderTrackerConfig()
        tracker_config.seed_frames = getattr(
            self.config, "seed_frames", tracker_config.seed_frames
        )
        tracker_config.min_snr = getattr(self.config, "min_snr", tracker_config.min_snr)
        processor = StreamingGuiderProcessor(
            tracker_config,
            on_combined_offset=self._handle_combined_offset,
            on_visit_start=self._handle_visit_start,
            on_visit_complete=self._handle_visit_complete,
        )
        self.processor = processor
        self.log.info(
            "Guider pipeline ready: seed_frames=%d min_snr=%g simulation_mode=%d",
            tracker_config.seed_frames,
            tracker_config.min_snr,
            self.simulation_mode,
        )
        if self.simulation_mode != 0:
            self._accept_stamps = True
            return

        import guiderGDS

        # Preload the camera outside the DAQ callback so its first lookup
        # does not delay the SDK acknowledgement. Capture this processor:
        # cancelling the await does not cancel work already in the executor.
        await self._event_loop.run_in_executor(
            None, lambda: processor.combiner.orientation.camera
        )
        self.stamp_source = guiderGDS.DaqStampSource(
            partition=self.config.partition,
            locations=guiderGDS.LocationSet.any(),
        )
        self._accept_stamps = True
        self.stamp_source.start_stamp_stream(self.process_stamp)
        self.log.info(
            "Subscribed to guider stamps on partition %r.", self.config.partition
        )

    async def stop_guiding(self):
        """Join the source, finalize a healthy run, and drain publication.

        Concurrent callers share the same cleanup task. Cancellation of a
        caller waits for that cleanup before propagating; it does not cancel
        the executor's native worker join. The CSC waits up to ten seconds
        for the join. On timeout, it retains the source and pending future,
        skips finalization, and requires a process restart. The native join
        continues in the executor; this deadline does not bound process
        exit time. Publication then drains for up to ten more seconds,
        after which its writer is cancelled and pending
        samples are abandoned. A writer that ignores cancellation remains
        owned and prevents another run until it exits.

        Raises
        ------
        BaseException
            The original run failure, if any. Secondary cleanup failures
            are logged and attached as notes. Repeated calls preserve the
            outcome and never retry finalization or an unconfirmed join.
        """
        if self._stop_guiding_task is None:
            self._stop_guiding_task = asyncio.create_task(self._stop_guiding())
        try:
            await asyncio.shield(self._stop_guiding_task)
        except asyncio.CancelledError:
            # Keep the join owned even if disable or close is cancelled.
            try:
                await asyncio.shield(self._stop_guiding_task)
            except (Exception, asyncio.CancelledError):
                self.log.exception("Run cleanup failed while its caller was cancelled.")
            raise

    async def _stop_guiding(self):
        """Join the worker before finalizing and draining publication."""
        async with self._lifecycle_lock:
            self._accept_stamps = False
            if self.stamp_source is not None:
                try:
                    # asyncio.wait leaves the executor future running on
                    # timeout. Retain both it and the source until exit.
                    self._source_stop_future = (
                        asyncio.get_running_loop().run_in_executor(
                            None, self.stamp_source.stop_stamp_stream
                        )
                    )
                    done, _ = await asyncio.wait(
                        {self._source_stop_future}, timeout=SOURCE_STOP_TIMEOUT
                    )
                    if not done:
                        self._source_stop_future.add_done_callback(
                            self._source_stop_done_after_timeout
                        )
                        raise TimeoutError(
                            "DAQ source did not stop within "
                            f"{SOURCE_STOP_TIMEOUT:g} seconds; native join is still "
                            "pending. Restart the CSC process."
                        )
                    self._source_stop_future.result()
                except Exception as error:
                    self._source_stop_failed = True
                    self._record_run_error(error, ErrorCode.SHUTDOWN)
                    self.log.exception(
                        "DAQ worker termination is unconfirmed; restart the CSC process."
                    )
                else:
                    self._source_stop_future = None
                    self.stamp_source = None

            if self.processor is not None and not self._source_stop_failed:
                try:
                    if self._run_error is None:
                        self._completed_visit_sensors = ":".join(
                            sorted(self._visit_sensors)
                        )
                        self._completed_visit_identity = self._latest_identity
                        self.processor.finalize()
                except Exception as error:
                    self._record_run_error(error, ErrorCode.STAMP_PROCESSING)
                    self.log.exception("Failed to finalize the guider pipeline.")
                finally:
                    self.processor = None

            if self._source_stop_failed:
                # An in-flight callback may still run. Close its publication
                # gate before placing a sentinel behind the accepted samples.
                self._accept_publication = False
            await asyncio.sleep(0)
            self._accept_publication = False
            queue = self._publish_queue
            task = self._publish_task
            if queue is not None and task is not None:
                queue.put_nowait(None)
                done, _ = await asyncio.wait({task}, timeout=PUBLISH_DRAIN_TIMEOUT)
                if not done:
                    error = TimeoutError(
                        "Guider publication did not drain within "
                        f"{PUBLISH_DRAIN_TIMEOUT:g} seconds."
                    )
                    self._record_run_error(error, ErrorCode.SHUTDOWN)
                    pending = max(0, queue.qsize() - 1)
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(None)
                    self.log.error(
                        "%s Abandoning the current write and %d queued payloads.",
                        error,
                        pending,
                    )
                    task.cancel()
                    await asyncio.sleep(0)
                if task.done():
                    if not task.cancelled():
                        task.result()
                    self._publish_task = None
                self._publish_queue = None
            if self._run_error is not None and not isinstance(
                self._run_error, asyncio.CancelledError
            ):
                raise self._run_error

    def _source_stop_done_after_timeout(self, future):
        """Observe a late native stop without reopening the failed run."""
        try:
            future.result()
        except (Exception, asyncio.CancelledError) as error:
            self._record_run_error(error, ErrorCode.SHUTDOWN)
        else:
            self.log.warning(
                "DAQ source stop completed after its deadline; "
                "a CSC process restart is still required."
            )

    def _record_run_error(self, error, code):
        """Retain the first exception and report secondary cleanup errors."""
        # Worker processing can fail while the event loop handles a failed
        # source stop. Keep the first error and its code together.
        with self._error_lock:
            if self._run_error is None:
                self._run_error = error
                self._run_error_code = code
                return
        if self._run_error is not error:
            self._run_error.add_note(f"Additional Guider failure: {error!r}")
            self.log.error(
                "Additional Guider failure: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def process_stamp(self, pixels, metadata):
        """Handle one guider stamp.

        Called on the DAQ C++ worker thread. Records the per-stamp
        identity (sequence, timestamp, obsid) and ROI geometry the
        SAL topics need: the pipeline consumes the DAQ metadata for
        visit boundaries and sensor orientation and produces only
        science results keyed by ``stamp_index``. The stamp is then
        handed to the streaming processor, whose callbacks enqueue
        the SAL payloads.

        Parameters
        ----------
        pixels : `numpy.ndarray`
            Two-dimensional stamp image; borrowed pixels are copied by the
            tracker when it needs to retain them for reference selection.
        metadata : `guiderGDS.StampMetadata`
            Sensor, acquisition and sequence identifiers, timestamp and ROI.

        Notes
        -----
        Calls must be serialized in DAQ acquisition order. Processing errors
        are retained and fault the CSC on its event loop; subsequent stamps
        are ignored until a new run is started after successful cleanup.
        """
        processor = self.processor
        if not self._accept_stamps or processor is None or self._run_error is not None:
            return
        try:
            sequence = int(getattr(metadata, "sequence", 0))
            if self._visit_sequence is None or sequence != self._visit_sequence:
                self._reset_visit_bookkeeping(sequence)
            self._record_stamp_metadata(pixels, metadata)
            processor.on_stamp(pixels, metadata)
        except Exception as error:
            # The C++ callback catches Python exceptions. Schedule the fault
            # here so a failed pipeline cannot silently remain ENABLED.
            # Shutdown must join this worker from a different thread.
            self._record_run_error(error, ErrorCode.STAMP_PROCESSING)
            self.log.exception("Failed to process a guider stamp.")
            self._event_loop.call_soon_threadsafe(self._schedule_processing_fault)

    def _schedule_processing_fault(self):
        """Report a worker failure from the asyncio event loop."""
        if self._closing or self._processing_fault_task is not None:
            return
        self._processing_fault_task = asyncio.create_task(
            self.fault(
                code=self._run_error_code,
                report=f"Failed to process a guider stamp: {self._run_error}",
            )
        )

    def _reset_visit_bookkeeping(self, sequence):
        """Start bookkeeping for a new visit (DAQ sequence).

        Called at the first stamp of the new visit, before the
        processor ends the previous one, so the finishing visit's
        sensors and identity are snapshotted here for the STOP state
        event published by `_handle_visit_complete`.
        """
        self._completed_visit_sensors = ":".join(sorted(self._visit_sensors))
        self._completed_visit_identity = self._latest_identity
        self._visit_sequence = sequence
        self._last_stamp_index = None
        self._stamp_metadata_by_index = {}
        self._visit_sensors = set()
        self._visit_rois = {}
        self._roi_shape = (0, 0)
        self._latest_identity = None
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
        # Metadata keeps the raw DAQ counter; the processor unwraps it.
        # Ordered delivery means a different index begins an acquisition.
        # Replace any seed-era record with this raw index after rollover.
        if stamp_index != self._last_stamp_index:
            self._stamp_metadata_by_index[stamp_index] = dict(
                identity=identity, sensors=set()
            )
            self._last_stamp_index = stamp_index
        record = self._stamp_metadata_by_index[stamp_index]
        record["sensors"].add(sensor_name)
        self._latest_identity = record["identity"]

        self._visit_sensors.add(sensor_name)
        if len(self._visit_sensors) > NUMBER_OF_GUIDER_SENSORS:
            raise ValueError("Guider topics support at most eight sensors.")

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
            self._queue_payload, queue, topic_attribute_name, payload
        )

    def _queue_payload(self, queue, topic_attribute_name, payload):
        """Accept a scheduled payload only while its run's queue is open."""
        if self._accept_publication and queue is self._publish_queue:
            queue.put_nowait((topic_attribute_name, payload))

    async def _publish_loop(self):
        """Drain the publish queue, writing one topic per item."""
        queue = self._publish_queue
        while True:
            item = await queue.get()
            if item is None:
                return
            topic_attribute_name, payload = item
            try:
                topic = getattr(self, topic_attribute_name)
                await topic.set_write(**payload)
            except Exception:
                self.log.exception(
                    "Failed to write %s; sample is not retried.", topic_attribute_name
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
                sensors=":".join(sorted(self._visit_sensors)),
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
        ROIs received so far have been recorded), then the STAMP
        state event, the offsets telemetry, and the summary and
        per-guider results for the acquisition.
        """
        record = self._stamp_metadata_by_index.pop(combined.stamp_index % 65536, None)
        if record is not None:
            identity = record["identity"]
            sensors = ":".join(sorted(record["sensors"]))
        else:
            identity = dict(
                self._default_identity(), stamp=combined.stamp_index % 65536
            )
            sensors = ":".join(sorted(combined.measurements))

        self.log.info(
            "Guider combined result: seqno=%d stamp=%d obsid=%r "
            "timestamp=%.9f x_mm=%.9g y_mm=%.9g "
            "x_standard_error_mm=%.9g y_standard_error_mm=%.9g "
            "x_scatter_mm=%.9g y_scatter_mm=%.9g "
            "valid_sensors=%d total_sensors=%d",
            identity["seqno"],
            identity["stamp"],
            identity["obsid"],
            identity["timestamp"],
            combined.combined_dx * PIXEL_SIZE_MM,
            combined.combined_dy * PIXEL_SIZE_MM,
            combined.error_dx * PIXEL_SIZE_MM,
            combined.error_dy * PIXEL_SIZE_MM,
            combined.scatter_dx * PIXEL_SIZE_MM,
            combined.scatter_dy * PIXEL_SIZE_MM,
            combined.n_valid,
            combined.n_total,
        )

        if not self._series_metadata_published:
            self._series_metadata_published = True
            self._enqueue(
                "evt_seriesMetadata",
                dict(force_output=True, **self._build_series_metadata_payload()),
            )

        self._enqueue(
            "evt_stateMetadata",
            dict(status=GuiderStatus.STAMP, sensors=sensors, **identity),
        )
        self._enqueue(
            "tel_offsets",
            dict(
                x=combined.combined_dx * PIXEL_SIZE_MM,
                y=combined.combined_dy * PIXEL_SIZE_MM,
                # The pipeline does not measure a rotation offset yet.
                rotation=math.nan,
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
        """Build ROI arrays in sorted sensor order for the Guider XML."""
        sensor_names = sorted(self._visit_rois)
        rois = [self._visit_rois[name] for name in sensor_names]
        return dict(
            roiCommonNrows=self._roi_shape[0],
            roiCommonNcols=self._roi_shape[1],
            # The integration time is not carried by the stamp
            # stream; it is part of the DAQ series configuration.
            roiCommonIntegration=0,
            sensor=":".join(sensor_names),
            segment=padded_array([roi[0] for roi in rois], 0),
            startrow=padded_array([roi[1] for roi in rois], 0),
            startcol=padded_array([roi[2] for roi in rois], 0),
            # Split-ROI stamps are not flagged by the stamp stream.
            splitroi=padded_array([], False),
        )

    def _build_summary_results_payload(self, combined, identity):
        """Build camera-frame translations in mm and per-sensor quality."""
        # qualityFlag is per sensor, in the same sorted-sensor order
        # as the perGuiderResults arrays: 1 passed the quality cuts,
        # 0 failed.
        quality_flags = [
            (
                1.0
                if combined.measurements[name].state == MeasurementState.PASSED_QUALITY
                else 0.0
            )
            for name in sorted(combined.measurements)
        ]
        return dict(
            deltaX=combined.combined_dx * PIXEL_SIZE_MM,
            deltaY=combined.combined_dy * PIXEL_SIZE_MM,
            # The pipeline does not measure a rotation offset yet.
            deltaRotation=math.nan,
            goodStamps=combined.n_valid,
            qualityFlag=padded_array(quality_flags, math.nan),
            **identity,
        )

    def _build_per_guider_results_payload(self, combined, identity):
        """Build offsets in mm and amplifier-frame moments in mm squared."""
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

        # HSM reports sigma = det(M) ** (1/4), so the half-trace is
        # sigma^2 / sqrt(1 - e1^2 - e2^2), not simply sigma^2 for an
        # elliptical star. Ixx - Iyy = e1 (Ixx + Iyy), and
        # 2 Ixy = e2 (Ixx + Iyy). These remain in the amplifier (ROI)
        # frame, not rotated to DVCS.
        moment_xx = []
        moment_yy = []
        moment_xy = []
        for measurement in measurements:
            distortion = math.hypot(measurement.e1, measurement.e2)
            if (
                measurement.state
                in (MeasurementState.CONVERGED, MeasurementState.PASSED_QUALITY)
                and math.isfinite(measurement.fwhm)
                and measurement.fwhm > 0
                and distortion < 1
            ):
                sigma = measurement.fwhm / FWHM_PER_SIGMA * PIXEL_SIZE_MM
                variance = sigma * sigma / math.sqrt(1.0 - distortion * distortion)
                if not math.isfinite(variance):
                    variance = math.nan
            else:
                variance = math.nan
            moment_xx.append(variance * (1.0 + measurement.e1))
            moment_yy.append(variance * (1.0 - measurement.e1))
            moment_xy.append(variance * measurement.e2)

        nan_array = padded_array([], math.nan)
        return dict(
            sensor=":".join(sensor_names),
            # Flux and its uncertainty are not retained in the measurement.
            flux=nan_array,
            fluxErr=nan_array,
            # Absolute DVCS centroid positions need the focal-plane
            # transform of the camera model (ROI origin + amplifier
            # to focal plane); not implemented yet. The guiding
            # quantity, the offset from the reference, is published
            # in centroidDx/centroidDy in camera-frame mm.
            centroidX=nan_array,
            centroidY=nan_array,
            centroidXErr=nan_array,
            centroidYErr=nan_array,
            centroidDx=padded_array(centroid_dx, math.nan),
            centroidDy=padded_array(centroid_dy, math.nan),
            momentXx=padded_array(moment_xx, math.nan),
            momentYy=padded_array(moment_yy, math.nan),
            momentXy=padded_array(moment_xy, math.nan),
            centroidFitQuality=padded_array(
                [
                    1.0 if measurement.state == MeasurementState.PASSED_QUALITY else 0.0
                    for measurement in measurements
                ],
                math.nan,
            ),
            **identity,
        )


def run_guider_csc() -> None:
    """Run the Guider CSC."""
    asyncio.run(GuiderCsc.amain(index=True))
