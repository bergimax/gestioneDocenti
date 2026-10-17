"""Both env example files keep one comment block per variable, and document
every setting that config/settings*.py reads."""

import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = [".env.example", "deploy/env.production.example"]

VARIABLE_LINE = re.compile(r"^#?\s?([A-Z][A-Z0-9_]*)=")
OPENING_LINE = re.compile(r"^# (Required|Optional)")
# NAME strings passed to get_*() or read with os.environ.get(); assignments to
# os.environ[...] (settings_test.py) are not reads.
SETTINGS_READ = re.compile(
    r"""(?:get_\w+\(\s*os\.environ,\s*|os\.environ\.get\(\s*)"([A-Z][A-Z0-9_]*)\""""
)

# Read by config/settings*.py but deliberately absent from the production file.
PRODUCTION_ALLOWED_MISSING = {
    "DATABASE_URL": "docker-compose.yml builds it from the POSTGRES_* variables",
    "BACKUP_MARKER_FILE": "docker-compose.yml sets it itself for web",
}


def parse(name):
    """Return (variable lines, attached comment blocks).

    A variable line is NAME=value or a commented-out # NAME=value. A comment
    block is a run of # lines that are not variable lines; one followed by a
    blank line (file or section header) belongs to no variable and is skipped.
    """
    lines = (ROOT / name).read_text().splitlines()
    variables = []  # (index, name)
    blocks = []  # (variable name, [comment lines])
    for i, line in enumerate(lines):
        m = VARIABLE_LINE.match(line)
        if not m:
            continue
        variables.append((i, m.group(1)))
        block = []
        j = i - 1
        while j >= 0 and lines[j].startswith("#") and not VARIABLE_LINE.match(lines[j]):
            block.insert(0, lines[j])
            j -= 1
        blocks.append((m.group(1), block))
    return lines, variables, blocks


def block_above(name, variable):
    return "\n".join(dict(parse(name)[2])[variable])


@pytest.mark.parametrize("name", FILES)
def test_every_variable_has_its_own_comment_block(name):
    lines, variables, blocks = parse(name)
    for (i, var), (_, block) in zip(variables, blocks, strict=True):
        # (a) directly preceded by a comment line; (b) so never two variables
        # under one block, since a variable line above is not a comment.
        assert block, f"{name}: {var} (line {i + 1}) has no comment directly above"
        # (c) exactly one Required/Optional opening line per block
        openers = [line for line in block if OPENING_LINE.match(line)]
        assert len(openers) == 1, (
            f"{name}: comment of {var} has {len(openers)} Required/Optional lines"
        )


@pytest.mark.parametrize("name", FILES)
def test_no_variable_is_defined_twice(name):
    names = [var for _, var in parse(name)[1]]
    assert [n for n, c in Counter(names).items() if c > 1] == []


def settings_names():
    names = set()
    for path in (ROOT / "config").glob("settings*.py"):
        names |= set(SETTINGS_READ.findall(path.read_text()))
    return names


def test_settings_scan_finds_the_known_names():
    assert {"SECRET_KEY", "BACKUP_STALE_HOURS", "LOGIN_MAX_FAILURES"} <= (
        settings_names()
    )


@pytest.mark.parametrize("name", FILES)
def test_every_setting_is_documented(name):
    documented = {var for _, var in parse(name)[1]}
    allowed = PRODUCTION_ALLOWED_MISSING if name.startswith("deploy/") else {}
    missing = settings_names() - documented - set(allowed)
    assert missing == set()


def test_production_allow_list_is_really_absent():
    documented = {var for _, var in parse("deploy/env.production.example")[1]}
    assert not documented & set(PRODUCTION_ALLOWED_MISSING)


@pytest.mark.parametrize(
    ("variable", "word"),
    [
        ("OFFSITE_RCLONE_REMOTE", "off-site"),
        ("BACKUP_STALE_HOURS", "STALE"),
        ("BACKUP_MARKER_FILE", "marker"),
    ],
)
def test_backup_comments_name_their_own_variable(variable, word):
    assert word.lower() in block_above(".env.example", variable).lower()


def test_backup_variables_keep_their_order():
    _, variables, _ = parse(".env.example")
    order = [v for _, v in variables]
    wanted = [
        "OFFSITE_RCLONE_REMOTE",
        "BACKUP_STALE_HOURS",
        "BACKUP_MARKER_FILE",
        "OFFSITE_RCLONE_CONFIG",
    ]
    assert [v for v in order if v in wanted] == wanted
