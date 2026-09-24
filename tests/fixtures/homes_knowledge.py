"""SYNTHETIC knowledge fixture for Homes Prime tests.

Every property, fact, and page here is invented test data (RC2 §23.5 permits clearly labeled
synthetic fixtures). It is deliberately NOT the Ray-approved R1 corpus, whose approval covers local
R1 testing of its exact bytes only; that corpus is exercised solely by the opt-in test in
tests/unit/test_knowledge.py when HOMES_PRIME_R1_CORPUS_PATH points at it.
"""

from __future__ import annotations

import json
from pathlib import Path

from utopia_homes_prime.knowledge.projection import canonical_corpus_digest

HOST = "https://www.utopiahomes.com"
EFFECTIVE = "2026-01-01T00:00:00Z"


def _property(slug: str, label: str, facts: dict[str, object], text: str) -> dict[str, object]:
    return {
        "id": f"{slug}-capacity",
        "service_line": "homes",
        "kind": "fact",
        "title": f"{label} capacity",
        "approved_text": text,
        "aliases": [f"{label} size"],
        "topics": ["capacity", "parking"],
        "route": "property",
        "property_slug": slug,
        "property_facts": facts,
        "source": {"id": f"{slug}-page", "label": label, "href": f"{HOST}/stays/{slug}"},
        "links": [
            {"id": f"{slug}-page-link", "label": f"Explore {label}", "href": f"{HOST}/stays/{slug}"}
        ],
        "effective_from": EFFECTIVE,
        "direct_answer": True,
    }


SYNTHETIC_ENTRIES: list[dict[str, object]] = [
    _property(
        "harbor-light",
        "Harbor Light",
        {
            "max_guests": 12,
            "parking_spaces": 3,
            "has_pool": True,
            "has_hot_tub": False,
            "bedrooms": 5,
            "bathrooms": 3,
            "pets_allowed": False,
        },
        "Harbor Light welcomes up to 12 guests, has 5 bedrooms, and parking for 3 cars.",
    ),
    _property(
        "dune-cottage",
        "Dune Cottage",
        {
            "max_guests": 6,
            "parking_spaces": 2,
            "has_pool": False,
            "has_hot_tub": True,
            "bedrooms": 3,
            "bathrooms": 2,
            "pets_allowed": True,
        },
        "Dune Cottage welcomes up to 6 guests, has 3 bedrooms, and parking for 2 cars.",
    ),
    {
        "id": "collection-overview",
        "service_line": "homes",
        "kind": "navigation",
        "title": "Synthetic collection overview",
        "approved_text": "The synthetic test collection includes Harbor Light and Dune Cottage.",
        "aliases": ["homes"],
        "topics": ["collection"],
        "route": "stays",
        "source": {"id": "stays-page", "label": "Utopia stays", "href": f"{HOST}/stays"},
        "links": [{"id": "stays-page-link", "label": "Explore all stays", "href": f"{HOST}/stays"}],
        "effective_from": EFFECTIVE,
        "direct_answer": True,
    },
    {
        "id": "contact-utopia",
        "service_line": "general",
        "kind": "call_to_action",
        "title": "Contact Utopia",
        "approved_text": "Visitors can reach Utopia through the contact page.",
        "aliases": ["contact"],
        "topics": ["contact"],
        "route": "contact",
        "source": {"id": "contact-page", "label": "Contact Utopia", "href": f"{HOST}/contact"},
        "links": [
            {"id": "contact-page-link", "label": "Contact Utopia", "href": f"{HOST}/contact"}
        ],
        "effective_from": EFFECTIVE,
        "direct_answer": True,
    },
    {
        "id": "expired-promotion",
        "service_line": "homes",
        "kind": "policy",
        "title": "Expired synthetic promotion",
        "approved_text": "A synthetic promotion that is no longer effective.",
        "aliases": [],
        "topics": ["promotion"],
        "route": "stays",
        "source": {"id": "stays-page", "label": "Utopia stays", "href": f"{HOST}/stays"},
        "links": [],
        "effective_from": "2025-01-01T00:00:00Z",
        "effective_until": "2025-06-01T00:00:00Z",
        "direct_answer": False,
    },
]


def padded_corpus(total_entries: int) -> dict[str, object]:
    """The synthetic corpus padded with synthetic description entries (each with one link) until
    it holds `total_entries` entries, of which all but the expired one are effective."""
    document = synthetic_corpus()
    entries: list[dict[str, object]] = document["entries"]  # type: ignore[assignment]
    index = 0
    while len(entries) < total_entries:
        index += 1
        entries.append(
            {
                "id": f"synthetic-note-{index}",
                "service_line": "general",
                "kind": "description",
                "title": f"Synthetic note {index}",
                "approved_text": f"Synthetic approved note number {index}.",
                "aliases": [],
                "topics": ["synthetic"],
                "route": "about",
                "source": {"id": "about-page", "label": "About", "href": f"{HOST}/about"},
                "links": [
                    {
                        "id": f"synthetic-note-{index}-link",
                        "label": "Read more",
                        "href": f"{HOST}/about/note-{index}",
                    }
                ],
                "effective_from": EFFECTIVE,
                "direct_answer": False,
            }
        )
    return document


def write_corpus(directory: Path, document: dict[str, object]) -> tuple[Path, str]:
    path = directory / "synthetic-knowledge.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, canonical_corpus_digest(document)


def synthetic_corpus() -> dict[str, object]:
    return {
        "schema": "lucy-public-knowledge-v1",
        "entries": json.loads(json.dumps(SYNTHETIC_ENTRIES)),
    }


def write_synthetic_corpus(directory: Path) -> tuple[Path, str]:
    document = synthetic_corpus()
    path = directory / "synthetic-knowledge.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, canonical_corpus_digest(document)
