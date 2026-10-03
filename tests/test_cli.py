import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from drivecheck.__main__ import main


def test_port_conflict_never_starts_station_lifespan(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(
        "drivecheck.__main__.uvicorn.Server.run", lambda *a, **k: started.append(True)
    )
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "drivecheck",
                "--demo",
                "--host",
                "127.0.0.1",
                "--port",
                str(occupied.getsockname()[1]),
                "--data-dir",
                str(tmp_path),
            ],
        )
        with pytest.raises(SystemExit):
            main()
    assert not started
    assert not (tmp_path / "drivecheck.sqlite3").exists()


def test_endpoint_reserved_before_start_and_closed_afterward(tmp_path, monkeypatch):
    listeners = []

    def run(server, sockets):
        assert len(sockets) == 1 and sockets[0].getsockname()[1] > 0
        assert not (tmp_path / "drivecheck.sqlite3").exists()
        listeners.extend(sockets)

    monkeypatch.setattr("drivecheck.__main__.uvicorn.Server.run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["drivecheck", "--demo", "--host", "127.0.0.1", "--port", "0", "--data-dir", str(tmp_path)],
    )
    main()
    assert listeners[0].fileno() == -1


def test_shutdown_with_live_dashboard_stream_completes_cleanup(tmp_path):
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    token = "isolated-shutdown-test-token"
    env = {k: v for k, v in os.environ.items() if not k.startswith("DRIVECHECK_")}
    env["DRIVECHECK_API_KEY"] = token
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "drivecheck",
                "--demo",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--data-dir",
                str(tmp_path),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        assert client.get("/api/health").status_code == 200
                        break
                    except httpx.ConnectError:
                        assert time.monotonic() < deadline
                        time.sleep(0.05)
                assert client.post("/api/login", json={"token": token}).status_code == 200
                with client.stream("GET", "/api/events") as stream:
                    lines = stream.iter_lines()
                    assert next(lines) == "event: state"
                    assert not stream.is_closed
                    process.terminate()
                    process.wait(timeout=12)
            assert process.returncode in {0, -signal.SIGTERM}
            log.seek(0)
            assert "Application shutdown complete." in log.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
