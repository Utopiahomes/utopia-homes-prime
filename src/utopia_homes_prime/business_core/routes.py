"""HTTP routes for the business core.

- `/internal/v1/...` is for Utopia Lucy only: bearer token, never exposed to guests.
- `/business/v1/public/properties` is the public property feed (the facts already shown on the
  website), for the website and the guest-answer knowledge builder.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from utopia_homes_prime.business_core.knowledge_items import (
    InvalidItem,
    ItemNotFound,
    KnowledgeItemStore,
)
from utopia_homes_prime.business_core.records import public_feed
from utopia_homes_prime.business_core.store import (
    Change,
    InvalidChange,
    PropertyNotFound,
    PropertyStore,
)

INTERNAL_PREFIX = "/internal/v1/"


class PropertyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: dict[str, Any] = Field(min_length=1, max_length=20)
    changed_by: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=500)


class ConfirmFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[str] = Field(min_length=1, max_length=30)
    confirmed_by: str = Field(min_length=1, max_length=120)


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: dict[str, Any]
    created_by: str = Field(min_length=1, max_length=120)
    confirm: bool = False
    """True when a person stated this (it is then confirmed); false for extracted proposals."""


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: dict[str, Any] = Field(min_length=1, max_length=20)
    changed_by: str = Field(min_length=1, max_length=120)
    confirm: bool = False


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


def _change(c: Change) -> dict[str, Any]:
    return {
        "field": c.field,
        "old": c.old,
        "new": c.new,
        "changed_by": c.changed_by,
        "reason": c.reason,
        "changed_at": c.changed_at.isoformat(),
    }


def register_business_routes(
    app: FastAPI,
    *,
    store: PropertyStore,
    lucy_token: str,
    items: KnowledgeItemStore | None = None,
) -> None:
    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        return header.startswith("Bearer ") and hmac.compare_digest(
            header[len("Bearer ") :].encode(), lucy_token.encode()
        )

    @app.middleware("http")
    async def _internal_auth(request: Request, call_next: Any) -> Any:
        if request.url.path.startswith(INTERNAL_PREFIX) and not authorized(request):
            return _error(401, "unauthorized", "a valid business token is required")
        return await call_next(request)

    @app.get("/internal/v1/properties")
    async def list_properties() -> JSONResponse:
        records = await run_in_threadpool(store.all)
        return JSONResponse(
            {
                "properties": [
                    {
                        "slug": r.slug,
                        "name": r.name,
                        "status": r.status,
                        "city": r.city,
                        "max_guests": r.max_guests,
                        "bedrooms": r.bedrooms,
                        "bathrooms": r.bathrooms,
                    }
                    for r in records
                ]
            }
        )

    @app.get("/internal/v1/properties/{slug}")
    async def get_property(slug: str) -> JSONResponse:
        try:
            record = await run_in_threadpool(store.get, slug)
            confirmed = await run_in_threadpool(store.confirmations, slug)
        except PropertyNotFound:
            return _error(404, "not_found", f"no property with slug {slug!r}")
        return JSONResponse(
            {
                "property": record.model_dump(mode="json"),
                "confirmed": confirmed,
                "unconfirmed_fields": sorted(set(record.model_dump()) - set(confirmed) - {"slug"}),
            }
        )

    @app.post("/internal/v1/properties/{slug}/confirm")
    async def confirm_property(slug: str, body: ConfirmFields) -> JSONResponse:
        try:
            confirmed = await run_in_threadpool(
                lambda: store.confirm(slug, body.fields, confirmed_by=body.confirmed_by)
            )
        except PropertyNotFound:
            return _error(404, "not_found", f"no property with slug {slug!r}")
        except InvalidChange as exc:
            return _error(422, "invalid_change", str(exc))
        return JSONResponse({"confirmed": confirmed})

    @app.patch("/internal/v1/properties/{slug}")
    async def update_property(slug: str, body: PropertyUpdate) -> JSONResponse:
        try:
            record, changes = await run_in_threadpool(
                lambda: store.update(
                    slug, body.changes, changed_by=body.changed_by, reason=body.reason
                )
            )
        except PropertyNotFound:
            return _error(404, "not_found", f"no property with slug {slug!r}")
        except InvalidChange as exc:
            return _error(422, "invalid_change", str(exc))
        return JSONResponse(
            {"property": record.model_dump(mode="json"), "changes": [_change(c) for c in changes]}
        )

    @app.get("/internal/v1/properties/{slug}/history")
    async def property_history(slug: str, limit: int = 20) -> JSONResponse:
        try:
            changes = await run_in_threadpool(store.history, slug, max(1, min(limit, 100)))
        except PropertyNotFound:
            return _error(404, "not_found", f"no property with slug {slug!r}")
        return JSONResponse({"changes": [_change(c) for c in changes]})

    if items is not None:
        register_item_routes(app, items)

    @app.get("/business/v1/public/properties")
    async def public_properties() -> JSONResponse:
        records = await run_in_threadpool(store.all)
        return JSONResponse(public_feed(records), headers={"Cache-Control": "public, max-age=60"})


def register_item_routes(app: FastAPI, items: KnowledgeItemStore) -> None:
    @app.get("/internal/v1/knowledge")
    async def search_items(
        property: str | None = None,
        audience: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 50,
    ) -> JSONResponse:
        found = await run_in_threadpool(
            lambda: items.search(
                property_slug=property,
                audiences=tuple(audience.split(",")) if audience else None,
                statuses=tuple(status.split(",")) if status else None,
                query=q,
                limit=max(1, min(limit, 500)),
            )
        )
        return JSONResponse({"items": [i.model_dump(mode="json") for i in found]})

    @app.post("/internal/v1/knowledge")
    async def create_item(body: ItemCreate) -> JSONResponse:
        try:
            item = await run_in_threadpool(
                lambda: items.create(body.item, created_by=body.created_by, confirm=body.confirm)
            )
        except InvalidItem as exc:
            return _error(422, "invalid_item", str(exc))
        return JSONResponse({"item": item.model_dump(mode="json")}, status_code=201)

    @app.patch("/internal/v1/knowledge/{item_id}")
    async def update_item(item_id: str, body: ItemUpdate) -> JSONResponse:
        try:
            item = await run_in_threadpool(
                lambda: items.update(
                    item_id, body.changes, changed_by=body.changed_by, confirm=body.confirm
                )
            )
        except ItemNotFound:
            return _error(404, "not_found", f"no knowledge item {item_id!r}")
        except InvalidItem as exc:
            return _error(422, "invalid_item", str(exc))
        return JSONResponse({"item": item.model_dump(mode="json")})
