"""The Homes Dragon materials registry: digest-pinned manifest, exact bytes, exact versions."""

from __future__ import annotations

import json
import shutil

import pytest
from fixtures.meeting import BRIEF, CHECKLIST, MANIFEST, manifest_digest

from utopia_homes_prime.meeting_assist.meeting_materials import (
    MaterialNotPermitted,
    MaterialRegistry,
    MaterialsUnavailable,
)

WORKSPACES_DIGESTS = {
    # The digests Workspaces computes over its own copies (utopia-wordspaces ccfe930, approved by
    # Ray). The adapter shows a material only when the Dragon reports the same id, version and
    # sha256.
    BRIEF["id"]: "dd5fcd2211a657a28a6b00d2a50097496379b811554577b60c97a73c36f4687e",
    CHECKLIST["id"]: "33407208407932fc651547e2f1f02210a71b37c987101d4ef08183c6309194ec",
}


def _load(path=MANIFEST, digest=None):
    return MaterialRegistry.load(
        path, allowed_manifest_digests=frozenset({digest or manifest_digest(path)})
    )


def test_vendored_demo_materials_match_the_workspaces_copies():
    registry = _load()
    assert {m.id: m.sha256 for m in registry.materials} == WORKSPACES_DIGESTS


def test_resolution_is_by_exact_id_and_version():
    registry = _load()
    assert [m.kind for m in registry.resolve([CHECKLIST, BRIEF])] == ["checklist", "brief"]
    for missing in ({"id": CHECKLIST["id"], "version": 2}, {"id": "unknown", "version": 1}):
        with pytest.raises(MaterialNotPermitted):
            registry.resolve([missing])


@pytest.fixture()
def copy(tmp_path):
    for path in MANIFEST.parent.iterdir():
        shutil.copyfile(path, tmp_path / path.name)
    return tmp_path


def _rewrite(copy, change):
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    change(manifest)
    (copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return copy / "manifest.json"


def test_tampered_material_bytes_are_refused(copy):
    with (copy / f"{BRIEF['id']}.md").open("ab") as handle:
        handle.write(b"\nThe fee is waived.\n")
    with pytest.raises(MaterialsUnavailable, match="differ"):
        _load(copy / "manifest.json")


def test_a_manifest_outside_the_allowlist_is_refused():
    with pytest.raises(MaterialsUnavailable, match="allowlisted"):
        _load(digest="0" * 64)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda m: m["materials"][0].update(file="../secret.md"), "file name"),
        (lambda m: m["materials"][0].update(kind="contract"), "kind"),
        (lambda m: m["materials"][0].update(version=True), "version"),
        (lambda m: m["materials"].append(dict(m["materials"][0])), "duplicate"),
        (lambda m: m.update(extra=1), "unknown members"),
        (lambda m: m["materials"][0].update(approved=True), "members"),
    ],
)
def test_malformed_manifests_are_refused(copy, change, match):
    path = _rewrite(copy, change)
    with pytest.raises(MaterialsUnavailable, match=match):
        _load(path)
