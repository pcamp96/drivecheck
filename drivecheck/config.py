"""Local runtime configuration. Secrets never belong in the repository."""

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def boolean(value: str | None, default: bool = False) -> bool:
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


@dataclass
class Config:
    data_dir: Path
    demo: bool = True
    allow_destructive: bool = False
    scan_interval: float = 5
    api_key: str = ""
    public_origin: str = ""
    secure_cookie: bool = False
    demo_step_seconds: float = 0
    headless: bool = False
    notification_wait_seconds: float = 30
    notification_env: dict | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "Config":
        provider = os.getenv("DRIVECHECK_NOTIFICATION_PROVIDER")
        notification_env = None
        if provider is not None:
            notification_env = {
                "provider": provider.lower(),
                "enabled": provider.lower() != "none",
                "discord_webhook": os.getenv("DRIVECHECK_DISCORD_WEBHOOK", ""),
                "telegram_token": os.getenv("DRIVECHECK_TELEGRAM_TOKEN", ""),
                "telegram_chat_id": os.getenv("DRIVECHECK_TELEGRAM_CHAT_ID", ""),
                "telegram_user_id": os.getenv("DRIVECHECK_TELEGRAM_USER_ID", ""),
                "notify_started": boolean(os.getenv("DRIVECHECK_NOTIFY_STARTED")),
                "notify_ready": boolean(os.getenv("DRIVECHECK_NOTIFY_READY"), True),
            }
        return cls(
            data_dir=Path(os.getenv("DRIVECHECK_DATA_DIR", "data")).expanduser().resolve(),
            demo=boolean(os.getenv("DRIVECHECK_DEMO"), True),
            allow_destructive=boolean(os.getenv("DRIVECHECK_ALLOW_DESTRUCTIVE")),
            scan_interval=max(1, float(os.getenv("DRIVECHECK_SCAN_INTERVAL", "5"))),
            api_key=os.getenv("DRIVECHECK_API_KEY", ""),
            public_origin=os.getenv("DRIVECHECK_PUBLIC_ORIGIN", "").rstrip("/"),
            secure_cookie=boolean(os.getenv("DRIVECHECK_SECURE_COOKIE")),
            demo_step_seconds=max(0, float(os.getenv("DRIVECHECK_DEMO_STEP_SECONDS", "0.25"))),
            headless=boolean(os.getenv("DRIVECHECK_HEADLESS")),
            notification_wait_seconds=max(
                0, float(os.getenv("DRIVECHECK_NOTIFICATION_WAIT_SECONDS", "30"))
            ),
            notification_env=notification_env,
        )

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        key_path = self.data_dir / "access-token"
        if not self.api_key:
            if key_path.exists():
                self.api_key = key_path.read_text().strip()
            else:
                self.api_key = secrets.token_urlsafe(32)
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(self.api_key + "\n")
        if len(self.api_key) < 16:
            raise ValueError("DRIVECHECK_API_KEY must contain at least 16 characters")


DEFAULT_SETTINGS = {
    "auto_test": False,
    "auto_eject": False,
    "auto_eject_delay_seconds": 180,
    "notifications": {
        "provider": "none",
        "enabled": False,
        "discord_webhook": "",
        "telegram_token": "",
        "telegram_chat_id": "",
        "telegram_user_id": "",
        "notify_started": False,
        "notify_ready": True,
    },
}


class Settings:
    def __init__(self, config: Config):
        self.config = config
        self.path = config.data_dir / "settings.json"
        self.value = json.loads(json.dumps(DEFAULT_SETTINGS))
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            self.value["auto_test"] = bool(saved.get("auto_test", False))
            self.value["auto_eject"] = bool(saved.get("auto_eject", False))
            self.value["auto_eject_delay_seconds"] = saved.get("auto_eject_delay_seconds", 180)
            self.value["notifications"].update(saved.get("notifications", {}))
        if config.notification_env is not None:
            self.value["notifications"].update(config.notification_env)
            from drivecheck.notifications import validate_settings

            validate_settings(self.value["notifications"])
        if config.headless:
            self.value["auto_test"] = True
            self.value["auto_eject"] = True

    def public(self) -> dict:
        value = json.loads(json.dumps(self.value))
        notice = value["notifications"]
        notice["discord_configured"] = bool(notice["discord_webhook"])
        notice["telegram_configured"] = bool(
            notice["telegram_token"] and notice["telegram_chat_id"]
        )
        notice["discord_webhook"] = ""
        notice["telegram_token"] = ""
        value["allow_destructive"] = self.config.allow_destructive
        value["demo"] = self.config.demo
        value["headless"] = self.config.headless
        value["notifications_from_env"] = self.config.notification_env is not None
        return value

    def update(self, patch: dict) -> dict:
        # Validate a copy before committing a single atomic replacement.
        value = json.loads(json.dumps(self.value))
        for key in ("auto_test", "auto_eject"):
            if key in patch:
                if self.config.headless and not patch[key]:
                    raise ValueError("Headless mode requires automatic testing and safe eject")
                value[key] = patch[key]
        if "auto_eject_delay_seconds" in patch:
            delay = patch["auto_eject_delay_seconds"]
            if type(delay) is not int or not 0 <= delay <= 3600:
                raise ValueError("Choose an eject delay from 0 to 3600 seconds")
            value["auto_eject_delay_seconds"] = delay
        if patch.get("notifications") and self.config.notification_env is not None:
            raise ValueError(
                "Notifications are managed by the station environment; edit its configuration and restart"
            )
        notice = value["notifications"]
        for key, item in patch.get("notifications", {}).items():
            if key not in notice or item is None:
                continue
            if key in {"discord_webhook", "telegram_token"} and not item:
                continue  # Blank input preserves a secret already on disk.
            notice[key] = item
        if patch.get("notifications", {}).get("clear_discord"):
            notice["discord_webhook"] = ""
        if patch.get("notifications", {}).get("clear_telegram"):
            notice["telegram_token"] = ""
            notice["telegram_chat_id"] = ""
            notice["telegram_user_id"] = ""
        from drivecheck.notifications import validate_settings

        validate_settings(notice)
        if self.config.headless and not notice["enabled"]:
            raise ValueError("Headless mode requires an enabled notification provider")
        atomic_json(self.path, value)
        self.value = value
        return self.public()
