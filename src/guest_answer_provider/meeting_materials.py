"""Homes-approved meeting materials for the Homes Dragon meeting operations.

A material is usable only by exact (id, version), from a manifest whose SHA-256 is allowlisted in
deployment configuration, and only when every file's bytes match the digest the manifest records.
An identifier alone grants nothing: an unknown version is refused, never upgraded or downgraded.
The digests reported by `GET /business/v1/meeting/identity` are over each file's exact bytes, so
Workspaces can verify that it displays the same version the Dragon used.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

MATERIAL_ID_RE: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MATERIAL_ID_MAX: Final = 96
MATERIAL_KINDS: Final = ("brief", "checklist")
MATERIAL_TITLE_MAX: Final = 160
MATERIAL_FILE_MAX_BYTES: Final = 32 * 1024
MATERIALS_MAX: Final = 8
_MANIFEST_KEYS: Final = frozenset({"format_version", "notice", "materials"})
_ENTRY_KEYS: Final = frozenset({"id", "version", "kind", "title", "file", "sha256"})
_FILE_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9.-]{0,127}\.md$")


class MaterialsUnavailable(ValueError):
    """The manifest or a material failed verification. Startup must fail closed."""


class MaterialNotPermitted(Exception):
    """A request named a material the Dragon does not hold at that exact version."""


@dataclass(frozen=True, slots=True)
class Material:
    id: str
    version: int
    kind: str
    title: str
    sha256: str
    text: str

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    def identity_entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "sha256": self.sha256,
            "title": self.title,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class MaterialRegistry:
    manifest_sha256: str
    materials: tuple[Material, ...]

    @classmethod
    def load(
        cls, manifest_path: Path, *, allowed_manifest_digests: frozenset[str]
    ) -> MaterialRegistry:
        try:
            raw = manifest_path.read_bytes()
        except OSError as exc:
            raise MaterialsUnavailable("materials manifest is unreadable") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if digest not in allowed_manifest_digests:
            raise MaterialsUnavailable("materials manifest digest is not allowlisted")
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MaterialsUnavailable("materials manifest is not valid JSON") from exc
        if not isinstance(manifest, dict) or not set(manifest) <= _MANIFEST_KEYS:
            raise MaterialsUnavailable("materials manifest has unknown members")
        if manifest.get("format_version") != 1:
            raise MaterialsUnavailable("unsupported materials manifest format")
        entries = manifest.get("materials")
        if not isinstance(entries, list) or len(entries) > MATERIALS_MAX:
            raise MaterialsUnavailable(f"materials must be a list of at most {MATERIALS_MAX}")

        materials: list[Material] = []
        seen: set[tuple[str, int]] = set()
        for entry in entries:
            material = _load_entry(entry, manifest_path.parent)
            if (material.id, material.version) in seen:
                raise MaterialsUnavailable("duplicate material id and version")
            seen.add((material.id, material.version))
            materials.append(material)
        return cls(manifest_sha256=digest, materials=tuple(materials))

    def identity_entries(self) -> list[dict[str, Any]]:
        return [material.identity_entry() for material in self.materials]

    def resolve(self, requested: Iterable[dict[str, Any]]) -> tuple[Material, ...]:
        """Exact (id, version) lookup for a schema-valid request list. Raises MaterialNotPermitted
        when any entry is not held at that exact version; never substitutes another version."""
        by_key = {(m.id, m.version): m for m in self.materials}
        resolved: list[Material] = []
        for item in requested:
            material = by_key.get((item["id"], item["version"]))
            if material is None:
                raise MaterialNotPermitted()
            resolved.append(material)
        return tuple(resolved)


def _load_entry(entry: object, directory: Path) -> Material:
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
        raise MaterialsUnavailable("material entry members are invalid")
    material_id, version = entry["id"], entry["version"]
    kind, title, file_name, recorded = entry["kind"], entry["title"], entry["file"], entry["sha256"]
    if (
        not isinstance(material_id, str)
        or len(material_id) > MATERIAL_ID_MAX
        or not MATERIAL_ID_RE.fullmatch(material_id)
    ):
        raise MaterialsUnavailable("material id is invalid")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise MaterialsUnavailable("material version must be a positive integer")
    if kind not in MATERIAL_KINDS:
        raise MaterialsUnavailable("material kind is invalid")
    if not isinstance(title, str) or not 1 <= len(title) <= MATERIAL_TITLE_MAX:
        raise MaterialsUnavailable("material title is invalid")
    # A plain file name next to the manifest: no directories, no traversal.
    if not isinstance(file_name, str) or not _FILE_NAME_RE.fullmatch(file_name):
        raise MaterialsUnavailable("material file name is invalid")
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        raise MaterialsUnavailable("material sha256 is invalid")
    try:
        data = (directory / file_name).read_bytes()
    except OSError as exc:
        raise MaterialsUnavailable("material file is unreadable") from exc
    if len(data) > MATERIAL_FILE_MAX_BYTES:
        raise MaterialsUnavailable("material file is too large")
    if hashlib.sha256(data).hexdigest() != recorded:
        raise MaterialsUnavailable("material bytes differ from the manifest digest")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MaterialsUnavailable("material is not UTF-8") from exc
    if "\x00" in text:
        raise MaterialsUnavailable("material contains NUL")
    return Material(material_id, version, kind, title, recorded, text)
