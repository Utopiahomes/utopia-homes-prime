"""Facts Ray confirmed that the live property records still have wrong.

Ray, 2026-11-18: Homes Prime still said Buttercup Beauty has 13 beds. He confirmed 12 on 2026-09-26,
along with 8 parking spaces at The Shamrock, and the website has shown both since. Guest answers are
built from these records, so each correction goes through the store and keeps its audit trail.

A correction applies only while the record still holds the old value, so a later edit is never
overwritten and running this twice changes nothing. Run as a job on the Homes Prime service:

    python -m utopia_homes_prime.business_core.fact_corrections          # show the facts
    python -m utopia_homes_prime.business_core.fact_corrections --apply
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any

from utopia_homes_prime.business_core.store import PostgresPropertyStore, PropertyStore

CHANGED_BY = "Ray"
REASON = "Ray confirmed this fact on 2026-09-26; corrected at his request on 2026-11-18."


@dataclass(frozen=True, slots=True)
class Correction:
    slug: str
    field: str
    old: Any
    new: Any


CORRECTIONS = (
    Correction("buttercup-beauty", "beds", 13, 12),
    Correction(
        "the-shamrock",
        "parking",
        "Parking for 6 cars.",
        "Parking for 8 cars: 3 in the garage and 5 in the driveway, plus street parking.",
    ),
    Correction(
        "the-shamrock",
        "unique_features",
        [
            "A restored piece of the historic Shamrock property",
            "Ten bedrooms, four living rooms, and two decks",
            "Garage and driveway parking for six cars",
        ],
        [
            "A restored piece of the historic Shamrock property",
            "Ten bedrooms, four living rooms, and two decks",
            "Garage and driveway parking for eight cars",
        ],
    ),
)


def apply_corrections(store: PropertyStore) -> list[str]:
    """One line per correction: what changed, or why it was left alone."""
    results = []
    for item in CORRECTIONS:
        current = store.get(item.slug).model_dump(mode="json")[item.field]
        label = f"{item.slug} {item.field}"
        if current == item.new:
            results.append(f"already correct: {label}")
        elif current != item.old:
            results.append(f"left alone, it was edited since: {label}")
        else:
            store.update(item.slug, {item.field: item.new}, changed_by=CHANGED_BY, reason=REASON)
            results.append(f"corrected: {label}")
    return results


def show(store: PropertyStore) -> list[str]:
    return [
        f"{record.slug}: beds={record.beds} parking={record.parking!r}" for record in store.all()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Correct facts Ray confirmed.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    store = PostgresPropertyStore(os.environ["UTOPIA_BUSINESS_DATABASE_URL"])
    if args.apply:
        for line in apply_corrections(store):
            print("FACTS", line, flush=True)
    for line in show(store):
        print("FACTS", line, flush=True)


if __name__ == "__main__":
    main()
