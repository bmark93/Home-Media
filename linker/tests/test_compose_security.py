"""Render configuration only; never contact the daemon or start services."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import PASSWORD, USER

REPO = Path(__file__).resolve().parents[2]
REQUIRED = {"LINKER_USER": USER, "LINKER_PASS": PASSWORD,
            "WATCHTOWER_API_TOKEN": "synthetic-watchtower-token-42"}


@pytest.fixture
def compose(tmp_path):
    docker = shutil.which("docker")
    assert docker, "Docker Compose CLI is required (no running daemon needed)"
    env_file = tmp_path / "synthetic.env"
    env_file.write_text("", encoding="utf-8")
    # Do not inherit user secrets, .env, Docker contexts or host home paths.
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "PROGRAMDATA", "PROGRAMFILES") if name in os.environ}
    env.update({"PWD": "/synthetic/project", "HOME": "/synthetic/home",
                "DOCKER_CONFIG": str(tmp_path / "docker"),
                "COMPOSE_CONVERT_WINDOWS_PATHS": "0"})

    def render(values=None, quiet=False):
        command = [docker, "compose", "--project-name", "security-test", "--project-directory",
                   str(tmp_path), "--env-file", str(env_file), "-f", str(REPO / "docker-compose.yml"), "config"]
        command += ["-q"] if quiet else ["--format", "json"]
        return subprocess.run(command, env=env | (REQUIRED if values is None else values),
                              cwd=tmp_path, capture_output=True, text=True, timeout=30,
                              check=False)
    return render


@pytest.mark.parametrize("overrides", [{}, {"BIND_ADDRESS": "192.0.2.10"},
                                       {"LINKER_BIND_ADDRESS": "192.0.2.20"}])
def test_compose_exposure(compose, overrides):
    result = compose(REQUIRED | overrides)
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    services = config["services"]
    port = services["linker"]["ports"][0]
    assert port["host_ip"] == overrides.get("LINKER_BIND_ADDRESS", "127.0.0.1")
    assert str(port["published"]) == "5050" and port["target"] == 8000
    assert not services["watchtower"].get("ports")
    for service in ("sonarr", "radarr", "prowlarr", "plex", "seerr", "grafana",
                    "prometheus", "cadvisor", "qbittorrent"):
        assert services[service]["ports"][0]["host_ip"] == overrides.get("BIND_ADDRESS", "0.0.0.0")
    for variable, value in REQUIRED.items():
        assert services["linker"]["environment"][variable] == value
    assert services["watchtower"]["environment"]["WATCHTOWER_HTTP_API_TOKEN"] == REQUIRED["WATCHTOWER_API_TOKEN"]
    assert config["secrets"]["linker_user"]["environment"] == "LINKER_USER"
    assert config["secrets"]["linker_pass"]["environment"] == "LINKER_PASS"
    assert services["prometheus"]["secrets"] == [
        {"source": "linker_user", "target": "/run/secrets/linker_user"},
        {"source": "linker_pass", "target": "/run/secrets/linker_pass"},
    ]
    assert compose(REQUIRED | overrides, quiet=True).returncode == 0


@pytest.mark.parametrize("variable", REQUIRED)
@pytest.mark.parametrize("empty", [True, False])
def test_compose_requires_secrets(compose, variable, empty):
    values = REQUIRED.copy()
    if empty:
        values[variable] = ""
    else:
        del values[variable]
    result = compose(values, quiet=True)
    assert result.returncode != 0
    assert variable in result.stderr
    assert "required" in result.stderr.lower()
    assert PASSWORD not in result.stderr


def test_example_has_no_working_secrets():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#"))
    for variable in REQUIRED:
        assert values.get(variable) == ""
    assert values["LINKER_BIND_ADDRESS"] == "127.0.0.1"


def test_watchtower_has_no_host_port(compose):
    result = compose()
    assert result.returncode == 0, result.stderr
    assert not json.loads(result.stdout)["services"]["watchtower"].get("ports")


def test_prometheus_linker_scrape_uses_secret_files():
    text = (REPO / "monitoring/prometheus/prometheus.yml").read_text(encoding="utf-8")
    assert "basic_auth:" in text
    assert "username_file: /run/secrets/linker_user" in text
    assert "password_file: /run/secrets/linker_pass" in text
    assert "username:" not in text and "password:" not in text
