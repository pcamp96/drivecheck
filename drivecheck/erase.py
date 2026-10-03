"""Parsing and recovery-journal helpers for destructive erase operations."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class AtaSecurity:
    supported: bool | None
    enabled: bool | None
    locked: bool | None
    frozen: bool | None
    erase_minutes: int | None

    @property
    def ready(self) -> bool:
        return (
            self.supported is True
            and self.enabled is False
            and self.locked is False
            and self.frozen is False
        )


def parse_hdparm_security(output: str) -> AtaSecurity:
    """Parse only hdparm's ATA Security section, failing closed on ambiguity."""
    section: list[str] = []
    active = False
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if stripped == "Security:":
            active = True
            continue
        if active and stripped.endswith(":") and not raw_line[:1].isspace():
            break
        if active:
            section.append(" ".join(stripped.lower().split()))

    def flag(positive: str, negative: str) -> bool | None:
        positive_seen = positive in section
        negative_seen = negative in section
        if positive_seen == negative_seen:
            return None
        return positive_seen

    supported = flag("supported", "not supported")
    enabled = flag("enabled", "not enabled")
    locked = flag("locked", "not locked")
    frozen = flag("frozen", "not frozen")
    minutes = None
    for line in section:
        match = re.search(r"(\d+)\s*min\s+for\s+security erase unit", line)
        if match:
            minutes = max(1, int(match.group(1)))
            break
    return AtaSecurity(supported, enabled, locked, frozen, minutes)


class RecoveryJournal:
    """Mode-0600 ATA password journal, retained while drive state is uncertain."""

    def __init__(self, directory: Path, identity: str) -> None:
        self.directory = directory
        self.path = directory / f"{identity}.json"

    def create(self, payload: dict[str, Any]) -> None:
        self._prepare_directory()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise RuntimeError("an ATA erase recovery journal already exists") from exc
        self._write_descriptor(descriptor, payload)
        self._sync_directory()

    def update(self, payload: dict[str, Any]) -> None:
        try:
            current = self.path.lstat()
        except OSError as exc:
            raise RuntimeError("ATA erase recovery journal is unavailable") from exc
        if not stat.S_ISREG(current.st_mode) or current.st_mode & 0o077:
            raise RuntimeError("ATA erase recovery journal is not a protected regular file")
        temporary = self.directory / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            self._write_descriptor(descriptor, payload)
            os.replace(temporary, self.path)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)
        self._sync_directory()

    def _prepare_directory(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeError("ATA erase recovery directory must be private")

    def _sync_directory(self) -> None:
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_descriptor(descriptor: int, payload: dict[str, Any]) -> None:
        try:
            os.fchmod(descriptor, 0o600)
            content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            written = 0
            while written < len(content):
                written += os.write(descriptor, content[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["AtaSecurity", "RecoveryJournal", "parse_hdparm_security"]
