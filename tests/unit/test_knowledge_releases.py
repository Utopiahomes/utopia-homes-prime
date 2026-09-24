"""The knowledge release register (cutover gate C2) matches the files it names: every recorded
release still admits under exactly its recorded digest."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REGISTER = json.loads((ROOT / "knowledge/releases.json").read_text(encoding="utf-8"))


def _tool():
    spec = importlib.util.spec_from_file_location(
        "knowledge_release", ROOT / "tools/knowledge_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["knowledge_release"] = module
    spec.loader.exec_module(module)
    return module


def test_release_ids_and_digests_are_unique():
    releases = REGISTER["releases"]
    assert releases
    assert len({r["release_id"] for r in releases}) == len(releases)
    assert len({r["canonical_digest"] for r in releases}) == len(releases)


@pytest.mark.parametrize("release", REGISTER["releases"], ids=lambda r: r["release_id"])
def test_every_recorded_release_matches_its_file(release):
    path = ROOT / release["file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == release["file_sha256"]
    result = _tool().check(path, frozenset({"www.utopiahomes.com"}))
    assert result["canonical_digest"] == release["canonical_digest"]
    assert release["approved_by"] and release["approved_on"]


def test_check_rejects_a_candidate_that_would_not_admit(tmp_path):
    document = json.loads((ROOT / REGISTER["releases"][0]["file"]).read_text(encoding="utf-8"))
    document["entries"][0]["source"]["href"] = "https://unapproved.example.com/page"
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(Exception, match="approved|hostname|host"):
        _tool().check(candidate, frozenset({"www.utopiahomes.com"}))


def test_diff_names_added_removed_and_changed_entries():
    tool = _tool()
    old = {"texts": {"a": "1", "b": "2"}}
    new = {"texts": {"b": "3", "c": "4"}}
    assert tool.diff(old, new) == {"added": ["c"], "removed": ["a"], "changed": ["b"]}
