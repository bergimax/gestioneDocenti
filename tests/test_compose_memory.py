import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {
    "postgres": ("POSTGRES", "256m", "128m"),
    "web": ("WEB", "256m", "160m"),
    "worker": ("WORKER", "448m", "256m"),
    "caddy": ("CADDY", "64m", "32m"),
}
UNITS = {"b": 1 / 2**20, "k": 1 / 1024, "m": 1, "g": 1024}


def _services():
    text = (ROOT / "docker-compose.yml").read_text()
    services = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    blocks = re.split(r"\n(?=  \w[\w-]*:\n)", "\n" + services)
    return {b.strip().split(":")[0]: b for b in blocks if b.strip()}


def _mb(value):
    return int(value[:-1]) * UNITS[value[-1]]


def _memory_keys(block):
    return dict(re.findall(r"^    (mem\w+|memswap_limit): (.+)$", block, re.M))


def test_every_service_has_limit_and_reservation_with_env_default():
    services = _services()
    assert set(services) == set(DEFAULTS)
    for name, (prefix, limit, reservation) in DEFAULTS.items():
        keys = _memory_keys(services[name])
        expected = {
            "mem_limit": f"${{{prefix}_MEM_LIMIT:-{limit}}}",
            "mem_reservation": f"${{{prefix}_MEM_RESERVATION:-{reservation}}}",
        }
        if name == "worker":
            expected["memswap_limit"] = expected["mem_limit"]
        assert keys == expected, name


def test_no_deploy_resources_and_no_swap_limit_outside_worker():
    text = (ROOT / "docker-compose.yml").read_text()
    assert not re.search(r"^\s*(deploy|resources):", text, re.M)
    assert "memswap_limit" not in "".join(
        b for n, b in _services().items() if n != "worker"
    )
    assert ":?" not in "".join(re.findall(r".*MEM_.*", text))


def test_default_numbers_are_sane():
    limits = {n: _mb(v[1]) for n, v in DEFAULTS.items()}
    reservations = {n: _mb(v[2]) for n, v in DEFAULTS.items()}
    for name in DEFAULTS:
        assert reservations[name] <= limits[name]
    assert sum(limits.values()) <= 1024
    assert limits["worker"] >= 350 + 64
    assert sum(reservations.values()) <= 640


def test_env_examples_document_the_eight_variables():
    for path in (ROOT / ".env.example", ROOT / "deploy" / "env.production.example"):
        lines = path.read_text().splitlines()
        for prefix, limit, reservation in DEFAULTS.values():
            for suffix, default in (("LIMIT", limit), ("RESERVATION", reservation)):
                var = f"{prefix}_MEM_{suffix}"
                assert f"# {var}={default}" in lines, (path.name, var)
                before = lines[lines.index(f"# {var}={default}") - 1]
                assert before.startswith("#") and "ptional" in before, var
                assert "b, k, m or g" in before, var
    prod = (ROOT / "deploy" / "env.production.example").read_text().splitlines()
    i = prod.index("# WORKER_MEM_LIMIT=448m")
    note = " ".join(prod[i - 3 : i])
    assert "swap" in note and "raise" in note.lower()


def test_readme_has_memory_limits_section():
    readme = (ROOT / "deploy" / "README.md").read_text()
    assert "## Memory limits" in readme
    section = readme.split("## Memory limits", 1)[1].split("\n## ", 1)[0]
    for prefix, *_ in DEFAULTS.values():
        assert (
            f"{prefix}_MEM_LIMIT" in section and f"{prefix}_MEM_RESERVATION" in section
        )
    assert "swap" in section and "#153" in section and "retry" in section.lower()
