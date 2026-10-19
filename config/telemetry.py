"""OpenTelemetry setup and the JSON log formatter.

Telemetry is off unless OTEL_EXPORTER_OTLP_ENDPOINT is set; the log formatter is
always on. Only ids, counts and redacted paths are recorded: no request bodies,
no query strings, no user names, no tokens.
"""

import json
import logging
import os
import time

from opentelemetry import trace

from availability.logging import redact

ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
_URL_ATTRIBUTES = ("http.target", "http.url", "url.path", "url.full", "url.query")


class JsonFormatter(logging.Formatter):
    """One JSON object per line; trace_id and span_id join it to the traces."""

    def __init__(self, service=None):
        super().__init__()
        self.service = service or os.environ.get("OTEL_SERVICE_NAME", "web")

    def format(self, record):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "message": redact(record.getMessage()),
        }
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            entry["trace_id"] = format(context.trace_id, "032x")
            entry["span_id"] = format(context.span_id, "016x")
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = record.exc_info[0].__name__
        return json.dumps(entry, ensure_ascii=False)


def _request_hook(span, request):
    """Replace the raw URL, which can carry a magic-link token, with a redacted one."""
    if not span.is_recording():
        return
    path = redact(request.path)
    for key in _URL_ATTRIBUTES:
        span.set_attribute(key, "" if key == "url.query" else path)


def setup_telemetry(service):
    """Export traces, metrics and logs over OTLP/HTTP. No-op without an endpoint."""
    if not os.environ.get(ENDPOINT_VAR):
        return False
    from opentelemetry import metrics
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": service})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
    )
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    global _log_handler
    _log_handler = LoggingHandler(logger_provider=logger_provider)
    _log_handler.addFilter(_RedactFilter())
    logging.getLogger().addHandler(_log_handler)

    DjangoInstrumentor().instrument(request_hook=_request_hook)
    # enable_commenter off and no statement capture of parameters: SQL values
    # can hold customer data, so only the statement text is traced.
    PsycopgInstrumentor().instrument()
    return True


_log_handler = None


def otel_log_handler():
    """Handler factory for settings.LOGGING.

    Django applies LOGGING after gunicorn's post_fork hook has set telemetry up and
    replaces the root handlers, so the OTLP handler is re-attached through it.
    """
    return _log_handler or logging.NullHandler()


class _RedactFilter(logging.Filter):
    def filter(self, record):
        record.msg, record.args = redact(record.getMessage()), ()
        # django.request attaches the request object, whose repr holds the full path
        record.__dict__.pop("request", None)
        return True
