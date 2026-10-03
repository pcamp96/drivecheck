import socket
import sys

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
