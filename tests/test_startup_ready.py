"""Native installer readiness checks against synthetic local HTTP services."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from drivecheck.app import create_app
from drivecheck.config import Config

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("wait_ready", ROOT / "scripts/wait-ready.py")
ready = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ready)


@pytest.fixture
def station(tmp_path, monkeypatch):
    options = {"pid": 777, "key": "synthetic-key-long-enough", "mode": "hardware"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = 200
            if self.path == "/api/health":
                body = {"status": "ok", "pid": options["pid"]}
            elif self.path == "/api/state" and self.headers.get("Authorization") == f"Bearer {options['key']}":
                body = {"mode": options["mode"]}
            else:
                status, body = 401, {"detail": "Unauthorized"}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    data = tmp_path / "private-data"
    data.mkdir()
    config = tmp_path / "drivecheck.env"
    config.write_text(f'DRIVECHECK_HOST=0.0.0.0\nDRIVECHECK_PORT={server.server_port}\nDRIVECHECK_DATA_DIR="{data}"\n')
    monkeypatch.setattr(ready, "service_pid", lambda _platform: 777)
    # A shell proxy must never receive the station's Authorization header.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    try:
        yield config, data / "access-token", options
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_waits_for_token_and_authenticates_configured_endpoint(station):
    config, token, options = station
    timer = threading.Timer(0.03, lambda: token.write_text(options["key"]))
    timer.start()
    try:
        base, token_path, fixed = ready.wait_ready(config, "linux", timeout=1)
        assert base.startswith("http://127.0.0.1:")
        assert token_path == token and not fixed
    finally:
        timer.join()


def test_fixed_key_is_valid_without_generated_token(station):
    config, token, options = station
    config.write_text(config.read_text() + f'DRIVECHECK_API_KEY="{options["key"]}"\n')
    assert ready.wait_ready(config, "macos", timeout=1)[2]
    assert not token.exists()


def test_linux_fixed_key_preserves_unquoted_hash(station):
    config, _, options = station
    options["key"] += "#suffix"
    config.write_text(config.read_text() + f'DRIVECHECK_API_KEY={options["key"]}\n')
    assert ready.wait_ready(config, "linux", timeout=1)[2]


@pytest.mark.parametrize("problem", ["missing_token", "wrong_pid", "wrong_key", "wrong_mode"])
def test_not_ready_conditions_cannot_report_success(station, problem):
    config, token, options = station
    if problem != "missing_token":
        token.write_text(options["key"])
    if problem == "wrong_pid":
        options["pid"] = 778
    elif problem == "wrong_key":
        options["key"] = "different-synthetic-key"
    elif problem == "wrong_mode":
        options["mode"] = "demo"
    with pytest.raises(RuntimeError, match="did not become ready"):
        ready.wait_ready(config, "linux", timeout=0.1)


def test_failed_service_is_reported_without_waiting(station, monkeypatch):
    config, _, _ = station

    def failed(_platform):
        raise RuntimeError("DriveCheck exited during startup")

    monkeypatch.setattr(ready, "service_pid", failed)
    with pytest.raises(RuntimeError, match="exited during startup"):
        ready.wait_ready(config, "linux", timeout=1)


def test_failure_output_never_includes_credentials(tmp_path):
    config = tmp_path / "drivecheck.env"
    secret = "a-private-key-value"
    config.write_text(f"DRIVECHECK_PORT={secret}\nDRIVECHECK_API_KEY={secret}\n")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/wait-ready.py"), str(config), "--platform", "linux"], text=True, capture_output=True)
    assert result.returncode == 1
    assert "journalctl" in result.stdout
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize("platform,output,pid", [
    ("linux", "ActiveState=active\nSubState=running\nMainPID=123\nResult=success", 123),
    ("macos", "org.drivecheck.station = {\n state = running\n pid = 456\n}", 456),
])
def test_native_service_pid_parsing(monkeypatch, platform, output, pid):
    monkeypatch.setattr(ready.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ""))
    assert ready.service_pid(platform) == pid


def test_health_reports_serving_process_pid(tmp_path):
    with TestClient(create_app(Config(tmp_path, api_key="synthetic-api-key-long"))) as client:
        health = client.get("/api/health").json()
        assert health["pid"] == os.getpid()
        assert health["mode"] == "demo"
