"""Homes-owned knowledge projection: digest pinning, effective time, withdrawal, destinations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fixtures.homes_knowledge import synthetic_corpus, write_synthetic_corpus

from utopia_homes_prime.knowledge.projection import (
    KnowledgeProjection,
    KnowledgeUnavailable,
    canonical_corpus_digest,
)

HOSTS = frozenset({"www.utopiahomes.com"})
NOW = datetime(2026, 9, 17, tzinfo=UTC)


def _load(path: Path, digest: str, *, withdrawn=frozenset(), release="knowledge-test.1"):
    return KnowledgeProjection.load(
        path,
        release_id=release,
        allowed_corpus_digests=frozenset({digest}),
        withdrawn_ids=frozenset(withdrawn),
        approved_hostnames=HOSTS,
    )


def test_digest_is_independent_of_member_and_entry_order(tmp_path):
    document = synthetic_corpus()
    reordered = {
        "entries": [dict(reversed(list(entry.items()))) for entry in reversed(document["entries"])],
        "schema": document["schema"],
    }
    assert canonical_corpus_digest(document) == canonical_corpus_digest(reordered)
    changed = synthetic_corpus()
    changed["entries"][0]["approved_text"] += " Changed."
    assert canonical_corpus_digest(changed) != canonical_corpus_digest(document)


def test_only_an_approved_digest_loads(tmp_path):
    path, digest = write_synthetic_corpus(tmp_path)
    _load(path, digest)
    with pytest.raises(KnowledgeUnavailable):
        _load(path, "0" * 64)


def test_expired_and_withdrawn_entries_are_excluded(tmp_path):
    path, digest = write_synthetic_corpus(tmp_path)
    effective = _load(path, digest).effective(NOW)
    assert "expired-promotion" not in effective.entries_by_id
    assert "harbor-light-capacity" in effective.entries_by_id

    withdrawn = _load(path, digest, withdrawn={"harbor-light-capacity"}).effective(NOW)
    assert "harbor-light-capacity" not in withdrawn.entries_by_id
    assert "harbor-light-page-link" not in withdrawn.links_by_id


def test_withdrawal_or_release_change_changes_replay_eligibility(tmp_path):
    path, digest = write_synthetic_corpus(tmp_path)
    base = _load(path, digest).eligibility_token
    assert _load(path, digest).eligibility_token == base
    assert _load(path, digest, withdrawn={"dune-cottage-capacity"}).eligibility_token != base
    assert _load(path, digest, release="knowledge-test.2").eligibility_token != base


def test_unknown_withdrawal_fails_closed(tmp_path):
    path, digest = write_synthetic_corpus(tmp_path)
    with pytest.raises(KnowledgeUnavailable):
        _load(path, digest, withdrawn={"not-in-corpus"})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["source"].__setitem__("href", "https://evil.example/stays/x"),
        lambda e: e["links"][0].__setitem__("href", "http://www.utopiahomes.com/stays/x"),
        lambda e: e["links"][0].__setitem__("href", "https://www.utopiahomes.com/x#frag"),
        lambda e: e["links"][0].__setitem__("label", "L" * 81),
        lambda e: e["source"].__setitem__("id", "bad--id"),
        lambda e: e.__setitem__("unexpected", True),
        lambda e: e.__setitem__("property_facts", None),
    ],
)
def test_unrenderable_or_unapproved_references_fail_at_load(tmp_path, mutate):
    document = synthetic_corpus()
    mutate(document["entries"][0])
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(KnowledgeUnavailable):
        _load(path, canonical_corpus_digest(document))


def test_all_entries_expired_is_unavailable(tmp_path):
    path, digest = write_synthetic_corpus(tmp_path)
    with pytest.raises(KnowledgeUnavailable):
        _load(path, digest).effective(datetime(2020, 1, 1, tzinfo=UTC))


def test_admitted_packet_is_bounded_at_the_rc1_enum_limit(tmp_path):
    """Lyra 2026-09-17: keep the admitted evidence set at 64 IDs or fewer, or fail closed; never
    drop the schema enum. The synthetic corpus has one expired entry, so N+1 entries admit N."""
    from fixtures.homes_knowledge import padded_corpus, write_corpus

    from utopia_homes_prime.inference import sme_wire
    from utopia_homes_prime.knowledge.projection import MAX_ADMITTED_IDS

    assert MAX_ADMITTED_IDS == sme_wire.SCHEMA_MAX_ENUM_MEMBERS == 64

    at_limit = tmp_path / "at-limit"
    at_limit.mkdir()
    path, digest = write_corpus(at_limit, padded_corpus(65))
    assert len(_load(path, digest).effective(NOW).entries_by_id) == 64

    over = tmp_path / "over"
    over.mkdir()
    path, digest = write_corpus(over, padded_corpus(66))
    with pytest.raises(KnowledgeUnavailable, match="64"):
        _load(path, digest).effective(NOW)

    # Withdrawal brings an oversized corpus back within the admitted bound.
    assert (
        len(_load(path, digest, withdrawn={"synthetic-note-1"}).effective(NOW).entries_by_id) == 64
    )


R1_PATH = Path(__file__).resolve().parents[2] / "knowledge/r1/utopia-public-knowledge.r1.json"
R1_APPROVED_DIGEST = "95e2e20a9e4a3786e3daa63a73bb5ff2866b5bae295e6dc138bf432e4361c422"


def test_r1_approved_corpus_loads_under_its_approved_digest():
    """The Homes-owned R1 corpus (approved 2026-09-12; moved byte-exact from cloud-hermes-lucy
    ba461b7). Local testing only: no test sends it to a provider, and a real-provider preview
    still needs an explicitly authorized knowledge release."""
    projection = _load(R1_PATH, R1_APPROVED_DIGEST, release="r1-local-test")
    effective = projection.effective(NOW)
    assert len(effective.entries_by_id) == 25
