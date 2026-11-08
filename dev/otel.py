import logging
import os
import threading

from livekit.agents.telemetry import set_tracer_provider
from opentelemetry import metrics
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


_lock = threading.Lock()
_done = False
_flush = None


def setup_telemetry(service_name=None):
    """Set telemetry up once per process; later calls return the same flush function.

    prewarm runs once per job runner, and on Windows the runners are threads of one
    process (on Linux each is its own process), so this is called a dozen times at
    startup. Setting the global providers twice only logs a warning, but every call
    would also add another root log handler and export each log line once per call.
    """
    global _done, _flush
    with _lock:
        if not _done:
            _flush = _setup(service_name or os.getenv("AGENT_NAME", "test-agent"))
            _done = True
        return _flush


def _setup(service_name):
    """Ship traces, metrics and logs to Grafana over OTLP/HTTP.

    Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (e.g. http://<MACHINE_HOST>:4318,
    the grafana service in private-ai) so the agent still runs without it. The
    exporters read that variable themselves and append /v1/traces etc.

    Returns a flush function, or None when telemetry is off. Call it on shutdown -
    batch exporters hold data for seconds, and a job process exits without waiting.
    """
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None

    resource = Resource.create({"service.name": service_name})

    traces = TracerProvider(resource=resource)
    traces.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    # livekit-agents emits its spans (session, llm/stt/tts nodes, tool calls) through
    # its own tracer; this is what points that tracer at ours.
    set_tracer_provider(traces)

    # livekit-agents creates its metric instruments (turn latency, token usage) from
    # the global meter, so the provider has to be the global one.
    reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meters = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meters)

    logs = LoggerProvider(resource=resource)
    logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logs)
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logs))

    def flush() -> None:
        for provider in (traces, meters, logs):
            provider.force_flush()

    return flush
