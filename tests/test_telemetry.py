import json
import logging

from opentelemetry.sdk.trace import TracerProvider

from config.telemetry import JsonFormatter, setup_telemetry


def _record(message, *args):
    return logging.LogRecord("t", logging.INFO, __file__, 1, message, args, None)


def test_json_line_has_fields_and_no_trace_without_span():
    entry = json.loads(JsonFormatter("web").format(_record("hello %s", "x")))
    assert entry["message"] == "hello x"
    assert entry["service"] == "web"
    assert "trace_id" not in entry


def test_json_line_carries_trace_id_of_current_span():
    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("s") as span:
        entry = json.loads(JsonFormatter("web").format(_record("in span")))
    assert entry["trace_id"] == format(span.get_span_context().trace_id, "032x")


def test_link_tokens_are_redacted_in_json_logs():
    line = JsonFormatter("web").format(_record("GET /l/SECRETTOKENVALUE123/"))
    assert "SECRETTOKENVALUE123" not in line


def test_setup_is_a_no_op_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert setup_telemetry("web") is False


def test_generation_outcome_separates_our_errors_from_bad_data():
    from types import SimpleNamespace

    from scheduling.models import JobStatus
    from scheduling.tasks import ERROR_UNEXPECTED, _outcome, infeasible_message

    def job(status, error=""):
        return SimpleNamespace(status=status, error=error)

    assert _outcome(None) == "missing"
    assert _outcome(job(JobStatus.DONE)) == "done"
    assert _outcome(job(JobStatus.FAILED, infeasible_message())) == "failed"
    assert _outcome(job(JobStatus.FAILED, f"{ERROR_UNEXPECTED} (boom)")) == "error"
