import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "relay", Path(__file__).resolve().parent.parent / "deploy/observability/relay.py"
)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def test_firing_alert_becomes_a_repository_dispatch():
    body = relay.dispatch_body(
        {
            "status": "firing",
            "commonLabels": {"alertname": "Pages are slow for users", "user": "x"},
            "commonAnnotations": {"summary": "p95 above 3 s"},
            "alerts": [{"valueString": "secret"}],
        }
    )
    assert body == {
        "event_type": "grafana-alert",
        "client_payload": {
            "title": "Pages are slow for users",
            "status": "firing",
            "summary": "p95 above 3 s",
        },
    }


def test_resolved_alert_is_ignored():
    assert relay.dispatch_body({"status": "resolved"}) is None
