"""Short-lived, one-time links for opening DriveCheck without a password."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

from drivecheck.config import Config


@dataclass(frozen=True)
class _AccessLink:
    run_id: str
    expires_at: float


class AccessLinks:
    """Issue and redeem short-lived bearer links kept only in process memory."""

    EXPIRY_SECONDS = 10 * 60
    MAX_LIVE_LINKS = 100

    def __init__(self, config: Config):
        self._origin = self._validate_origin(config.public_origin) if config.public_origin else ""
        self._links: dict[str, _AccessLink] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _validate_origin(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or any(character.isspace() for character in value)
        ):
            raise ValueError("DRIVECHECK_PUBLIC_ORIGIN must be a valid HTTP or HTTPS URL")
        if "?" in value or "#" in value:
            raise ValueError("DRIVECHECK_PUBLIC_ORIGIN cannot contain a query or fragment")
        try:
            parsed = urlsplit(value)
            # Accessing these properties performs urllib's port and address validation.
            hostname = parsed.hostname
            _port = parsed.port
        except ValueError as error:
            raise ValueError(
                "DRIVECHECK_PUBLIC_ORIGIN must be a valid HTTP or HTTPS URL"
            ) from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "DRIVECHECK_PUBLIC_ORIGIN must use HTTP or HTTPS without credentials, a path, a query, or a fragment"
            )
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _discard_expired(self, now: float) -> None:
        expired = [digest for digest, link in self._links.items() if link.expires_at <= now]
        for digest in expired:
            del self._links[digest]

    def issue(self, run_id: str = "") -> str:
        if not isinstance(run_id, str):
            raise TypeError("run_id must be a string")
        if not self._origin:
            return ""

        now = time.monotonic()
        with self._lock:
            self._discard_expired(now)
            while len(self._links) >= self.MAX_LIVE_LINKS:
                del self._links[next(iter(self._links))]
            while True:
                token = secrets.token_urlsafe(32)
                digest = self._digest(token)
                if digest not in self._links:
                    break
            self._links[digest] = _AccessLink(run_id, now + self.EXPIRY_SECONDS)

        fragment = urlencode({"access": token, "run": run_id})
        return f"{self._origin}/#{fragment}"

    def redeem(self, token: str) -> str | None:
        if not isinstance(token, str) or not token:
            return None

        digest = self._digest(token)
        now = time.monotonic()
        with self._lock:
            self._discard_expired(now)
            link = self._links.pop(digest, None)
        return None if link is None else link.run_id
