"""Fail-closed inspection of Linux block-device ownership claims."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RaidOwnership:
    blocked: bool
    releasable: bool
    detail: str
    arrays: list[dict[str, Any]]

    @classmethod
    def clear(cls) -> "RaidOwnership":
        return cls(False, False, "", [])


class RaidInspector:
    """Read kernel holder topology without changing block-device state."""

    def __init__(self, class_root: Path = Path("/sys/class/block")) -> None:
        self.class_root = class_root

    def inspect(
        self,
        disk_path: str,
        partition_paths: set[str],
        stacked_paths: set[str],
    ) -> RaidOwnership:
        disk_name = Path(disk_path).name
        selected_members = {Path(path).name for path in partition_paths}
        selected_nodes = {disk_name, *selected_members}
        stacked_nodes = {Path(path).name for path in stacked_paths}
        try:
            holders: set[str] = set()
            for name in selected_nodes:
                node = self._node(name)
                holders.update(self._links(node / "holders"))
        except OSError:
            return RaidOwnership(
                True,
                False,
                "Kernel ownership topology could not be read safely.",
                [],
            )

        if not holders:
            if stacked_paths:
                return RaidOwnership(
                    True,
                    False,
                    "A stacked block device is present but its kernel ownership could not be verified.",
                    [],
                )
            return RaidOwnership.clear()
        if not holders <= stacked_nodes:
            return RaidOwnership(
                True,
                False,
                "Kernel ownership and block-device topology do not agree.",
                [],
            )

        arrays = []
        for holder in sorted(holders):
            try:
                array = self._node(holder)
                md = array / "md"
                if not md.is_dir():
                    return RaidOwnership(
                        True,
                        False,
                        f"Block device /dev/{holder} is not an MD array and cannot be released automatically.",
                        arrays,
                    )
                state = (md / "array_state").read_text(encoding="utf-8").strip()
                members = self._links(array / "slaves")
                upper_holders = self._links(array / "holders")
            except OSError:
                return RaidOwnership(
                    True,
                    False,
                    f"Ownership details for /dev/{holder} could not be read safely.",
                    arrays,
                )
            record = {
                "path": f"/dev/{holder}",
                "state": state or "unknown",
                "members": [f"/dev/{name}" for name in sorted(members)],
            }
            arrays.append(record)
            if state != "inactive":
                return RaidOwnership(
                    True,
                    False,
                    f"{record['path']} is {state or 'unknown'}, not an inactive MD claim.",
                    arrays,
                )
            if upper_holders:
                return RaidOwnership(
                    True,
                    False,
                    f"{record['path']} has another block layer above it.",
                    arrays,
                )
            if not members or not selected_members or not members <= selected_members:
                return RaidOwnership(
                    True,
                    False,
                    f"{record['path']} includes a whole disk or a member from another drive.",
                    arrays,
                )

        return RaidOwnership(
            True,
            True,
            "Inactive Linux MD metadata currently holds this drive.",
            arrays,
        )

    def _node(self, name: str) -> Path:
        if not name or name in {".", ".."} or "/" in name:
            raise OSError("invalid block node")
        node = self.class_root / name
        return node.resolve(strict=True)

    def _links(self, directory: Path) -> set[str]:
        if not directory.is_dir():
            raise OSError("block relationship directory is unavailable")
        names = set()
        for entry in directory.iterdir():
            target = entry.resolve(strict=True)
            canonical = self._node(entry.name)
            if target != canonical:
                raise OSError("block relationship is not canonical")
            names.add(entry.name)
        return names


__all__ = ["RaidInspector", "RaidOwnership"]
