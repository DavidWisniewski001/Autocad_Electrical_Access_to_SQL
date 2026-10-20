"""M6: scripts/docker_env.py and compose.yaml are checked without Docker."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "docker_env.py"
COMPOSE = ROOT / "compose.yaml"


def _load_script():
    spec = importlib.util.spec_from_file_location("docker_env_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def denv():
    if not SCRIPT.is_file():
        pytest.skip("scripts/docker_env.py missing")
    return _load_script()


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60
    )


def _parse_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v.strip("'")
    return out


def test_passwords_meet_sql_server_complexity(denv):
    seen = set()
    for _ in range(200):
        p = denv.make_password()
        seen.add(p)
        assert len(p) == 24
        assert re.search(r"[A-Z]", p) and re.search(r"[a-z]", p) and re.search(r"\d", p)
        assert re.search(r"[^A-Za-z0-9]", p)
        assert not set(p) & set("$'\"\\#` \t\n"), p  # unsafe in .env / shells / compose
    assert len(seen) == 200  # random


def test_writes_env_to_tmp_path_with_both_passwords(tmp_path):
    if not SCRIPT.is_file():
        pytest.skip("scripts/docker_env.py missing")
    out = tmp_path / "sub" / ".env"
    r = _run("--out", str(out))
    assert r.returncode == 0, r.stderr
    env = _parse_env(out)
    assert {"MSSQL_SA_PASSWORD", "POSTGRES_PASSWORD"} <= set(env)
    assert env["MSSQL_SA_PASSWORD"] != env["POSTGRES_PASSWORD"]
    for var in ("MSSQL_SA_PASSWORD", "POSTGRES_PASSWORD"):
        assert len(env[var]) == 24
        assert env[var] not in r.stdout + r.stderr  # nothing secret is printed


def test_refuses_to_overwrite_without_force(tmp_path):
    if not SCRIPT.is_file():
        pytest.skip("scripts/docker_env.py missing")
    out = tmp_path / ".env"
    out.write_text("MSSQL_SA_PASSWORD='keep-me'\n", encoding="utf-8")
    r = _run("--out", str(out))
    assert r.returncode != 0
    assert "--force" in r.stderr
    assert out.read_text(encoding="utf-8") == "MSSQL_SA_PASSWORD='keep-me'\n"
    r = _run("--out", str(out), "--force")
    assert r.returncode == 0, r.stderr
    assert _parse_env(out)["MSSQL_SA_PASSWORD"] != "keep-me"


# ---- compose.yaml -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compose():
    yaml = pytest.importorskip("yaml", reason="PyYAML not installed")
    if not COMPOSE.is_file():
        pytest.skip("compose.yaml missing")
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_compose_services_exist(compose):
    assert {"mssql", "postgres", "tests"} <= set(compose["services"])


def test_compose_passwords_are_required_variables(compose):
    svc = compose["services"]
    for service, var in (
        ("mssql", "MSSQL_SA_PASSWORD"),
        ("postgres", "POSTGRES_PASSWORD"),
        ("tests", "MSSQL_SA_PASSWORD"),
        ("tests", "POSTGRES_PASSWORD"),
    ):
        value = str(svc[service]["environment"][var])
        assert re.fullmatch(r"\$\{" + var + r":\?[^}]+\}", value), (service, var, value)
    text = COMPOSE.read_text(encoding="utf-8")
    literal = re.search(r"^\s*\w*PASSWORD:[ \t]*(?!\$\{)[^\s#]", text, re.MULTILINE)
    assert not literal, "literal password in compose.yaml"


def test_compose_ports_bind_loopback_only(compose):
    for name in ("mssql", "postgres"):
        ports = compose["services"][name]["ports"]
        assert ports
        for p in ports:
            assert str(p).startswith("127.0.0.1:"), (name, p)
    assert "ports" not in compose["services"]["tests"]


def test_compose_tests_wait_for_healthy_servers(compose):
    dep = compose["services"]["tests"]["depends_on"]
    for name in ("mssql", "postgres"):
        assert dep[name]["condition"] == "service_healthy"
        assert "healthcheck" in compose["services"][name]


def test_compose_data_is_mounted_read_only(compose):
    vols = [str(v) for v in compose["services"]["tests"]["volumes"]]
    for target in ("/data", "/fixtures"):
        mounts = [v for v in vols if v.split(":")[1:2] == [target]]
        assert len(mounts) == 1, (target, vols)
        assert mounts[0].endswith(":ro"), mounts[0]
    assert any(v.startswith("./data_files:") for v in vols)
    env = compose["services"]["tests"]["environment"]
    assert env["ACADE2SQL_DATA_DIR"] == "/data"
    assert env["ACADE2SQL_TEST_FIXTURE_DIR"] == "/fixtures"
