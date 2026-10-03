import json

import pytest

from drivecheck.config import Config, Settings


def test_environment_notifications_redacted_and_managed(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVECHECK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DRIVECHECK_NOTIFICATION_PROVIDER", "telegram")
    monkeypatch.setenv("DRIVECHECK_TELEGRAM_TOKEN", "123:secret")
    monkeypatch.setenv("DRIVECHECK_TELEGRAM_CHAT_ID", "456")
    monkeypatch.setenv("DRIVECHECK_HEADLESS", "true")
    config = Config.from_env()
    settings = Settings(config)
    public = settings.public()
    assert public["headless"] and public["auto_test"] and public["auto_eject"]
    assert public["notifications_from_env"]
    assert "secret" not in json.dumps(public)
    assert "secret" not in repr(config)
    with pytest.raises(ValueError, match="environment"):
        settings.update({"notifications": {"enabled": False}})
    with pytest.raises(ValueError, match="Headless"):
        settings.update({"auto_eject": False})


def test_headless_cannot_remove_provider_and_auto_eject_persists(tmp_path):
    config = Config(tmp_path, headless=True)
    settings = Settings(config)
    settings.update(
        {
            "notifications": {
                "provider": "telegram",
                "enabled": True,
                "telegram_token": "123:secret",
                "telegram_chat_id": "456",
            }
        }
    )
    with pytest.raises(ValueError, match="enabled"):
        settings.update({"notifications": {"enabled": False}})
    assert Settings(Config(tmp_path)).public()["auto_eject"]


def test_invalid_environment_provider_fails_without_exposing_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVECHECK_NOTIFICATION_PROVIDER", "discord")
    monkeypatch.setenv("DRIVECHECK_DISCORD_WEBHOOK", "https://invalid.example/secret")
    config = Config.from_env()
    config.data_dir = tmp_path
    with pytest.raises(ValueError) as error:
        Settings(config)
    assert "secret" not in str(error.value)


def test_quick_action_delay_is_default_persisted_and_bounded(tmp_path):
    settings = Settings(Config(tmp_path))
    assert settings.public()["auto_eject_delay_seconds"] == 180
    settings.update({"auto_eject_delay_seconds": 90})
    assert Settings(Config(tmp_path)).public()["auto_eject_delay_seconds"] == 90
    for bad in (-1, 3601, True, "180"):
        with pytest.raises(ValueError, match="delay"):
            settings.update({"auto_eject_delay_seconds": bad})
