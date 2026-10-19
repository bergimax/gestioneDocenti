"""Alert relay: Grafana's webhook body -> GitHub repository_dispatch.

Grafana cannot send the body GitHub requires (`event_type`, `client_payload`), so
this stdlib-only service sits between them. It listens only on the internal
compose network, forwards firing alerts and ignores resolved ones, and sends the
alert name and summary only: no labels or values that could carry user data.

Environment: GITHUB_REPO (owner/repo), ONCALL_WEBHOOK_TOKEN (GitHub token with
Contents: read and write), GITHUB_API_URL (default https://api.github.com).
"""

import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


def dispatch_body(grafana):
    """The repository_dispatch body for a Grafana notification, or None to ignore it."""
    if grafana.get("status") != "firing":
        return None
    return {
        "event_type": "grafana-alert",
        "client_payload": {
            "title": grafana.get("commonLabels", {}).get("alertname", "alert"),
            "status": "firing",
            "summary": grafana.get("commonAnnotations", {}).get("summary", ""),
        },
    }


def send(body):
    request = urllib.request.Request(
        f"{API}/repos/{os.environ['GITHUB_REPO']}/dispatches",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ['ONCALL_WEBHOOK_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = dispatch_body(json.loads(self.rfile.read(length)))
            status = 204 if body is None else send(body)
            print(f"relay: {'ignored' if body is None else 'dispatched'}", flush=True)
        except Exception as exc:  # Grafana retries on 5xx
            print(f"relay: failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            status = 502
        self.send_response(status if status < 300 else 502)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler
    ).serve_forever()
