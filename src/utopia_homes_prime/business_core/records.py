"""The property record: the facts a guest can see on a property page.

Its shape matches the website's `utopia-homes-public-properties-v1` feed, so the website, the
knowledge builder, and Utopia Lucy all speak about a property the same way.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PUBLIC_FEED_SCHEMA = "utopia-homes-public-properties-v1"
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class AmenityGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    amenities: list[str] = Field(max_length=40)


class Room(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room: str = Field(min_length=1, max_length=80)
    beds: list[str] = Field(max_length=12)


class PropertyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    status: Literal["active", "hidden", "draft", "archived"] = "active"
    city: str = Field(min_length=1, max_length=80)
    state: str = Field(min_length=1, max_length=80)
    short_description: str = Field(max_length=400)
    full_description: str = Field(max_length=2000)
    max_guests: int | None = Field(default=None, ge=1, le=100)
    bedrooms: int | None = Field(default=None, ge=0, le=100)
    beds: int | None = Field(default=None, ge=0, le=200)
    bathrooms: float | None = Field(default=None, ge=0, le=100)
    amenities: list[AmenityGroup] = Field(default_factory=list, max_length=12)
    unique_features: list[str] = Field(default_factory=list, max_length=12)
    pet_policy: str = Field(max_length=400)
    parking: str = Field(max_length=400)
    accessibility: str = Field(max_length=600)
    # Stay rules and layout guests ask about most (Airbnb message analysis, 2026-09-26).
    check_in_time: str | None = Field(default=None, max_length=40)
    check_out_time: str | None = Field(default=None, max_length=40)
    min_age: int | None = Field(default=None, ge=18, le=30)
    min_stay: str | None = Field(default=None, max_length=300)
    beds_by_room: list[Room] = Field(default_factory=list, max_length=20)


EDITABLE_FIELDS = frozenset(PropertyRecord.model_fields) - {"slug"}


def public_feed(records: list[PropertyRecord]) -> dict[str, Any]:
    """The website feed shape, active properties only."""
    return {
        "schema": PUBLIC_FEED_SCHEMA,
        "properties": [
            {k: v for k, v in r.model_dump(mode="json").items() if k != "status"}
            for r in records
            if r.status == "active"
        ],
    }
