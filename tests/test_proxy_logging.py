import os
import re
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = (ROOT / "deploy" / "Caddyfile").read_text()


def _regexps():
    return re.findall(r"^\s*(\S+) regexp (\S+) (\S+)$", CADDYFILE, re.M)


def redact(field, text):
    for name, pattern, repl in _regexps():
        if name.endswith(field):
            text = re.sub(pattern, repl, text)
    return text


def _blocks(name):
    """Bodies of every top-level-or-nested `name { ... }` block."""
    out = []
    for m in re.finditer(rf"^\s*{name} \{{$", CADDYFILE, re.M):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(CADDYFILE[i], 0)
            i += 1
        out.append(CADDYFILE[m.end() : i - 1])
    return out


def test_every_log_block_imports_the_filter():
    blocks = _blocks("log")
    assert len(blocks) == 2  # global default logger and the site access log
    for body in blocks:
        assert body.strip() == "import redacted_log"
    snippet = _blocks(r"\(redacted_log\)")[0]
    assert "format filter" in snippet
    assert "wrap json" in snippet
    assert not re.search(r"format (console|json)", CADDYFILE)
    assert "output stdout" in snippet


@pytest.mark.parametrize(
    "field, text, expected",
    [
        ("uri", "/l/TOKEN/", "/l/REDACTED/"),
        ("uri", "/l/TOKEN/salva/", "/l/REDACTED/salva/"),
        ("uri", "/l/TOKEN/?x=1", "/l/REDACTED/?x=1"),
        ("uri", "/feeds/TOKEN.ics", "/feeds/REDACTED.ics"),
        ("Referer", "https://d.example/l/TOKEN/", "https://d.example/l/REDACTED/"),
        (
            "Referer",
            "https://d.example/l/TOKEN/?x=1",
            "https://d.example/l/REDACTED/?x=1",
        ),
        (
            "Referer",
            "https://d.example/feeds/TOKEN.ics",
            "https://d.example/feeds/REDACTED.ics",
        ),
        ("uri", "/feeds/", "/feeds/"),
        ("uri", "/feeds/rigenera/teacher/3/", "/feeds/rigenera/teacher/3/"),
        ("uri", "/disponibilita/link/", "/disponibilita/link/"),
    ],
)
def test_redaction_regexes(field, text, expected):
    result = redact(field, text)
    assert result == expected
    assert "TOKEN" not in result


def test_regexes_are_re2_compatible():
    patterns = [p for _, p, _ in _regexps()]
    assert patterns
    for p in patterns:
        assert not re.search(r"\(\?[=!<]|\\[1-9]", p)


def test_cookie_and_authorization_deleted():
    assert re.search(r"request>headers>Cookie delete", CADDYFILE)
    assert re.search(r"request>headers>Authorization delete", CADDYFILE)


def test_reverse_proxy_overwrites_forwarded_for():
    assert "header_up X-Forwarded-For {remote_host}" in CADDYFILE


@pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy not installed")
def test_caddyfile_validates():
    subprocess.run(
        ["caddy", "validate", "--config", "deploy/Caddyfile", "--adapter", "caddyfile"],
        cwd=ROOT,
        env={**os.environ, "DOMAIN": "school.test"},
        check=True,
    )


def test_gunicorn_conf_has_no_access_log():
    conf = runpy.run_path(str(ROOT / "deploy" / "gunicorn.conf.py"))
    assert conf["accesslog"] is None
    assert conf["bind"] == "0.0.0.0:8000"
    assert conf["workers"] == 2


def test_dockerfile_and_compose_use_the_conf():
    for name in ("Dockerfile", "docker-compose.yml"):
        text = (ROOT / name).read_text()
        assert '"-c", "deploy/gunicorn.conf.py"' in text
        assert "access-logfile" not in text
        assert "accesslog" not in text
