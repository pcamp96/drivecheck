import json
import sqlite3
import time

from fastapi.testclient import TestClient

from drivecheck.app import create_app
from drivecheck.config import Config

TOKEN = "test-station-token-long-enough"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def app(tmp_path, **kwargs):
    return create_app(Config(data_dir=tmp_path, api_key=TOKEN, **kwargs))


def wait_finished(client, run_id):
    for _ in range(100):
        run = client.get(f"/api/runs/{run_id}", headers=AUTH).json()
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.01)
    raise AssertionError("Demo run did not finish")


def test_auth_secret_redaction_and_csrf(tmp_path):
    with TestClient(app(tmp_path)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/state").status_code == 401
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        assert (
            client.post(
                "/api/login", json={"token": TOKEN}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        response = client.post("/api/login", json={"token": TOKEN})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/api/state").status_code == 200
        assert client.put("/api/settings", json={"auto_test": True}).status_code == 403
        assert (
            client.put(
                "/api/settings", json={"auto_test": True}, headers={"Origin": "http://testserver"}
            ).status_code
            == 200
        )
        assert TOKEN not in client.get("/api/state").text
        client.post("/api/logout", headers={"Origin": "http://testserver"})
        assert client.get("/api/state").status_code == 401


def test_demo_full_run_report_and_history_survive_restart(tmp_path):
    with TestClient(app(tmp_path)) as client:
        drive = client.get("/api/state", headers=AUTH).json()["drives"][0]
        response = client.post(
            "/api/runs", json={"drive_id": drive["id"], "profile": "extended"}, headers=AUTH
        )
        assert response.status_code == 200
        run = wait_finished(client, response.json()["id"])
        assert run["status"] == "passed"
        assert set(run["results"]) == {
            "smart_before",
            "benchmark",
            "self_test",
            "surface",
            "smart_after",
        }
        report = client.get(f"/api/runs/{run['id']}/report", headers=AUTH)
        assert report.json()["simulated"] is True
        assert "attachment" in report.headers["content-disposition"]
    with TestClient(app(tmp_path)) as client:
        state = client.get("/api/state", headers=AUTH).json()
        assert state["runs"][0]["id"] == run["id"]
        assert state["runs"][0]["status"] == "passed"


def test_destructive_requires_station_arm_and_exact_serial(tmp_path):
    with TestClient(app(tmp_path)) as client:
        drive = client.get("/api/state", headers=AUTH).json()["drives"][0]
        body = {
            "drive_id": drive["id"],
            "profile": "verify",
            "confirmation": f"ERASE {drive['serial']}",
        }
        assert client.post("/api/runs", json=body, headers=AUTH).status_code == 409
    with TestClient(app(tmp_path, allow_destructive=True)) as client:
        body["confirmation"] = "ERASE wrong-drive"
        assert client.post("/api/runs", json=body, headers=AUTH).status_code == 409
        body["confirmation"] = f"ERASE {drive['serial']}"
        response = client.post("/api/runs", json=body, headers=AUTH)
        assert response.status_code == 200
        assert wait_finished(client, response.json()["id"])["status"] == "passed"


def test_settings_secrets_preserved_atomic_and_invalid_inputs_redacted(tmp_path):
    secret = "https://discord.com/api/webhooks/123/testing_secret_token"
    with TestClient(app(tmp_path)) as client:
        response = client.put(
            "/api/settings",
            headers=AUTH,
            json={
                "notifications": {
                    "provider": "discord",
                    "enabled": True,
                    "discord_webhook": secret,
                }
            },
        )
        assert response.status_code == 200
        assert secret not in response.text
        assert response.json()["notifications"]["discord_configured"] is True
        response = client.put(
            "/api/settings", headers=AUTH, json={"notifications": {"discord_webhook": ""}}
        )
        assert response.status_code == 200
        assert (
            json.loads((tmp_path / "settings.json").read_text())["notifications"]["discord_webhook"]
            == secret
        )
        response = client.put(
            "/api/settings",
            headers=AUTH,
            json={
                "auto_test": True,
                "notifications": {"discord_webhook": "http://127.0.0.1/secret"},
            },
        )
        assert response.status_code == 422
        assert client.get("/api/state", headers=AUTH).json()["settings"]["auto_test"] is False
        response = client.put(
            "/api/settings", headers=AUTH, json={"notifications": {"telegram_token": [secret]}}
        )
        assert response.status_code == 422
        assert secret not in response.text
        assert (tmp_path / "settings.json").stat().st_mode & 0o777 == 0o600


def test_report_retest_is_authenticated_and_always_read_only(tmp_path):
    with TestClient(app(tmp_path, demo_step_seconds=0.001)) as client:
        drive = client.get("/api/state", headers=AUTH).json()["drives"][0]
        run = client.post(
            "/api/runs", headers=AUTH, json={"drive_id": drive["id"], "profile": "quick"}
        ).json()
        wait_finished(client, run["id"])
        path = f"/api/runs/{run['id']}/retest"
        assert client.post(path).status_code == 401
        assert client.post("/api/runs/missing/retest", headers=AUTH).status_code == 409
        response = client.post(path, headers=AUTH, json={"profile": "verify"})
        assert response.status_code == 200
        assert response.json()["run"]["profile"] == "extended"
        assert wait_finished(client, response.json()["run"]["id"])["status"] == "passed"


def test_readable_report_export_and_explicit_notification_attachment(tmp_path, monkeypatch):
    captured = []

    async def send(settings, message, transport=None, *, attachment=None):
        captured.append((message, attachment))

    monkeypatch.setattr("drivecheck.notifications.send", send)
    application = app(tmp_path, demo_step_seconds=0.001)
    with TestClient(application) as client:
        drive = client.get("/api/state", headers=AUTH).json()["drives"][0]
        run = client.post(
            "/api/runs", headers=AUTH, json={"drive_id": drive["id"], "profile": "quick"}
        ).json()
        wait_finished(client, run["id"])
        path = f"/api/runs/{run['id']}"
        assert client.get(path + "/report.txt").status_code == 401
        response = client.get(path + "/report.txt", headers=AUTH)
        assert response.status_code == 200 and response.headers["content-type"].startswith(
            "text/plain"
        )
        assert "VERDICT: PASSED" in response.text
        assert not response.text.startswith("{")
        assert client.post(path + "/notify", headers=AUTH).status_code == 409
        client.put(
            "/api/settings",
            headers=AUTH,
            json={
                "notifications": {
                    "provider": "telegram",
                    "enabled": True,
                    "telegram_token": "123:fake",
                    "telegram_chat_id": "-987",
                }
            },
        )
        queued = client.post(path + "/notify", headers=AUTH).json()
        with sqlite3.connect(tmp_path / "drivecheck.sqlite3") as database:
            for _ in range(100):
                delivered = database.execute(
                    "SELECT delivered FROM outbox WHERE id=?", (queued["notice_id"],)
                ).fetchone()[0]
                if delivered:
                    break
                time.sleep(0.01)
            assert delivered
        assert captured[-1][1]["filename"] == f"drivecheck-{run['id']}.txt"
        assert "VERDICT: PASSED" in captured[-1][1]["text"]
        assert "fake" not in captured[-1][1]["text"]


def test_auto_read_only_and_login_throttle(tmp_path):
    with TestClient(app(tmp_path, allow_destructive=True)) as client:
        client.put("/api/settings", headers=AUTH, json={"auto_test": True})
        client.post("/api/scan", headers=AUTH)
        state = client.get("/api/state", headers=AUTH).json()
        assert len(state["runs"]) == 1
        assert state["runs"][0]["profile"] == "extended"
        wait_finished(client, state["runs"][0]["id"])
        client.post("/api/scan", headers=AUTH)
        assert len(client.get("/api/state", headers=AUTH).json()["runs"]) == 1
        for _ in range(5):
            assert client.post("/api/login", json={"token": "incorrect"}).status_code == 401
        assert client.post("/api/login", json={"token": TOKEN}).status_code == 429


def test_invalid_authorization_never_falls_back_to_cookie_and_clear_secrets(tmp_path):
    with TestClient(app(tmp_path)) as client:
        client.post("/api/login", json={"token": TOKEN})
        assert (
            client.put(
                "/api/settings", json={"auto_test": True}, headers={"Authorization": "garbage"}
            ).status_code
            == 401
        )
        headers = {"Origin": "http://testserver"}
        client.put(
            "/api/settings",
            headers=headers,
            json={
                "notifications": {
                    "provider": "discord",
                    "enabled": True,
                    "discord_webhook": "https://discord.com/api/webhooks/123/secret",
                }
            },
        )
        response = client.put(
            "/api/settings",
            headers=headers,
            json={"notifications": {"enabled": False, "clear_discord": True}},
        )
        assert response.status_code == 200
        assert response.json()["notifications"]["discord_configured"] is False
        assert "secret" not in (tmp_path / "settings.json").read_text()


def test_non_ascii_login_is_rejected_cleanly(tmp_path):
    with TestClient(app(tmp_path)) as client:
        assert client.post("/api/login", json={"token": "incorrect-é"}).status_code == 401


def test_drive_release_authenticated_and_blocked_during_work(tmp_path):
    from drivecheck.hardware import Hardware

    class ReleaseHardware(Hardware):
        released = 0

        def capabilities(self):
            return {
                "platform": "fixture",
                "can_test": True,
                "can_verify": True,
                "can_unmount": True,
                "can_eject": True,
                "tools": {},
                "limitations": [],
            }

        async def eject(self, drive):
            self.released += 1
            return {"status": "ejected", "detail": "Fixture drive safely released"}

    hardware = ReleaseHardware(demo=True)
    instance = create_app(Config(tmp_path, api_key=TOKEN, demo_step_seconds=0.1), hardware)
    with TestClient(instance) as client:
        drive = client.get("/api/state", headers=AUTH).json()["drives"][0]
        endpoint = f"/api/drives/{drive['id']}/eject"
        assert client.post(endpoint).status_code == 401
        assert hardware.released == 0
        run = client.post(
            "/api/runs", json={"drive_id": drive["id"], "profile": "extended"}, headers=AUTH
        ).json()
        assert client.post(endpoint, headers=AUTH).status_code == 409
        assert hardware.released == 0
        client.post(f"/api/runs/{run['id']}/cancel", headers=AUTH)
        for _ in range(100):
            if client.get("/api/state", headers=AUTH).json()["system"]["active_run_id"] is None:
                break
            time.sleep(0.01)
        assert client.post(endpoint, headers=AUTH).status_code == 200
        assert hardware.released == 1
