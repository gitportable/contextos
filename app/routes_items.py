"""Owner-scoped item, Brief, and Ledger endpoints."""
import json
import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import cache, item_events
from app.auth import current_user
from app.crypto import TokenCryptoError, decrypt_token, encrypt_token
from app.database import get_db
from app.item_ranker import rank_item
from app.models import Item, ItemEvent, ItemSource, SlackConnection, SlackMessage, User, as_utc, utcnow
from app.serializers import message_dicts

log = logging.getLogger("contextos.items")
router = APIRouter(prefix="/api/items", tags=["items"])
STATES = {"OPEN", "ACKNOWLEDGED_PENDING", "WAITING_ON_OTHER", "RESOLVED", "MUTED"}
TRANSITIONS = {
    "OPEN": {"ACKNOWLEDGED_PENDING", "WAITING_ON_OTHER", "RESOLVED", "MUTED"},
    "ACKNOWLEDGED_PENDING": {"OPEN", "WAITING_ON_OTHER", "RESOLVED", "MUTED"},
    "WAITING_ON_OTHER": {"OPEN", "ACKNOWLEDGED_PENDING", "RESOLVED", "MUTED"},
    "RESOLVED": {"OPEN"},
    "MUTED": {"OPEN"},
}


class ItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    kind: Literal["commitment", "meeting", "follow_up"] = "commitment"
    direction: Literal["asked_of_me", "i_promised", "they_promised", "i_asked"]
    due_at: datetime | None = None
    due_kind: Literal["stated", "inferred", "defaulted", "none"] = "none"
    source_message_ids: list[int] = Field(default_factory=list, max_length=20)


class ItemPatch(BaseModel):
    expected_version: int = Field(ge=1)
    state: Literal["OPEN", "ACKNOWLEDGED_PENDING", "WAITING_ON_OTHER", "RESOLVED", "MUTED"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    due_at: datetime | None = None
    due_kind: Literal["stated", "inferred", "defaulted", "none"] | None = None


def _error(status: int, code: str, message: str):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _title(item: Item) -> str:
    try:
        return decrypt_token(item.title_encrypted)
    except TokenCryptoError as exc:
        log.error("item_title_decryption_failed item_id=%s", item.id)
        raise _error(503, "item_unavailable", "This item cannot be decrypted. Contact support before changing the encryption key.") from exc


def _sources(db: Session, item: Item, owner_id: int) -> list[SlackMessage]:
    return (db.query(SlackMessage)
            .join(ItemSource, ItemSource.message_id == SlackMessage.id)
            .filter(ItemSource.item_id == item.id, SlackMessage.owner_id == owner_id, SlackMessage.is_deleted.is_(False))
            .order_by(SlackMessage.message_created_at.asc(), SlackMessage.id.asc()).all())


def _serialize(db: Session, item: Item, owner_id: int) -> dict:
    sources = _sources(db, item, owner_id)
    connection_ids = {source.connection_id for source in sources}
    providers = {
        connection_id: provider
        for connection_id, provider in db.query(SlackConnection.id, SlackConnection.provider)
        .filter(SlackConnection.id.in_(connection_ids)).all()
    } if connection_ids else {}
    rank = rank_item(
        kind=item.kind,
        direction=item.direction,
        state=item.state,
        due_at=item.due_at,
        source_structures=[
            {"mentions_me": source.mentions_me, "is_dm": source.conv_type == "im",
             "provider": providers.get(source.connection_id)}
            for source in sources
        ],
    )
    return {
        "id": item.id,
        "title": _title(item),
        "kind": item.kind,
        "owner": item.owner_role,
        "direction": item.direction,
        "state": item.state,
        "due_at": item.due_at.isoformat() if item.due_at else None,
        "due_kind": item.due_kind,
        "version": item.version,
        "created_at": item.created_at.isoformat(),
        "score": rank["score"],
        "urgency": rank["urgency"],
        "explanation": rank["explanation"],
        "source_message_ids": [source.id for source in sources],
        "sources": message_dicts(db, sources),
    }


def _due(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _direction_owner(direction: str) -> str:
    return "other" if direction in ("they_promised", "i_asked") else "me"


@router.get("")
def list_items(state_filter: str = "active", limit: int = 100, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    limit = max(1, min(limit, 200))
    query = db.query(Item).filter(Item.owner_id == user.id)
    if state_filter == "active":
        query = query.filter(Item.state.in_(("OPEN", "ACKNOWLEDGED_PENDING", "WAITING_ON_OTHER")))
    elif state_filter not in STATES:
        raise _error(400, "bad_state", "state must be active or a valid item state.")
    else:
        query = query.filter(Item.state == state_filter)
    rows = query.order_by(Item.due_at.is_(None), Item.due_at.asc(), Item.created_at.desc()).limit(limit).all()
    cards = [_serialize(db, item, user.id) for item in rows]
    return sorted(cards, key=lambda card: (-card["score"], card["due_at"] is None, card["due_at"] or ""))


@router.get("/brief")
def brief(limit: int = 7, user: User = Depends(current_user), db: Session = Depends(get_db)):
    limit = max(1, min(limit, 7))
    rows = (db.query(Item).filter(Item.owner_id == user.id,
                                  Item.state.in_(("OPEN", "ACKNOWLEDGED_PENDING", "WAITING_ON_OTHER")))
            .all())
    cards = [_serialize(db, item, user.id) for item in rows]
    cards.sort(key=lambda card: (-card["score"], card["due_at"] is None, card["due_at"] or ""))
    return {
        "items": cards[:limit],
        "total_open": sum(card["state"] != "WAITING_ON_OTHER" for card in cards),
        "total_active": len(cards),
        "scoring": "rules_only",
    }


@router.post("")
def create_item(data: ItemCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    message_ids = sorted(set(data.source_message_ids))
    sources = (db.query(SlackMessage).filter(SlackMessage.owner_id == user.id, SlackMessage.id.in_(message_ids),
                                              SlackMessage.is_deleted.is_(False)).all() if message_ids else [])
    if len(sources) != len(message_ids):
        raise _error(404, "source_not_found", "One or more source messages are unavailable.")
    title = data.title.strip()
    if not title:
        raise _error(422, "empty_title", "Give this item a short title.")
    try:
        encrypted = encrypt_token(title)
    except TokenCryptoError as exc:
        log.error("item_encryption_unavailable owner_id=%s", user.id)
        raise _error(503, "encryption_unavailable", "Item storage is unavailable until encryption is configured.") from exc
    connection_ids = {source.connection_id for source in sources}
    item = Item(
        owner_id=user.id,
        connection_id=next(iter(connection_ids)) if len(connection_ids) == 1 else None,
        kind=data.kind,
        owner_role=_direction_owner(data.direction),
        direction=data.direction,
        state="OPEN",
        title_encrypted=encrypted,
        source_message_ids=json.dumps(message_ids, separators=(",", ":")),
        due_at=_due(data.due_at),
        due_kind=(data.due_kind if data.due_kind != "none" else "stated") if data.due_at else "none",
    )
    db.add(item)
    db.flush()
    for source in sources:
        db.add(ItemSource(item_id=item.id, message_id=source.id, connection_id=source.connection_id))
    item_events.record_item_event(db, owner_id=user.id, item_id=item.id, event_type="act", payload={"action": "created"})
    db.commit()
    cache.invalidate_user_caches(user.id)
    return _serialize(db, item, user.id)


@router.patch("/{item_id}")
def update_item(item_id: int, data: ItemPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.query(Item).filter_by(id=item_id, owner_id=user.id).first()
    if item is None:
        raise _error(404, "not_found", "Item not found.")
    changes = data.model_dump(exclude_unset=True)
    state = changes.pop("state", None)
    title = changes.pop("title", None)
    if state == item.state:
        state = None
    if state and state not in TRANSITIONS[item.state]:
        raise _error(409, "invalid_transition", f"Cannot move an item from {item.state} to {state}.")
    values = {}
    if state:
        values["state"] = state
    if "due_at" in changes:
        values["due_at"] = _due(changes["due_at"])
        if values["due_at"] is None:
            values["due_kind"] = "none"
        elif "due_kind" not in changes and item.due_kind == "none":
            values["due_kind"] = "stated"
    if "due_kind" in changes:
        target_due = values.get("due_at", item.due_at)
        if target_due is None and changes["due_kind"] != "none":
            raise _error(422, "due_kind_without_date", "A due-date kind requires a due date.")
        if target_due is not None and changes["due_kind"] == "none":
            raise _error(422, "date_without_due_kind", "A due date cannot use the none classification.")
        values["due_kind"] = changes["due_kind"]
    if title is not None:
        title = title.strip()
        if not title:
            raise _error(422, "empty_title", "Give this item a short title.")
        try:
            values["title_encrypted"] = encrypt_token(title)
        except TokenCryptoError as exc:
            raise _error(503, "encryption_unavailable", "Item storage is unavailable until encryption is configured.") from exc
    if not values:
        raise _error(400, "empty_update", "Change a state, title, or due date.")

    next_version = data.expected_version + 1
    result = db.execute(
        update(Item).where(Item.id == item_id, Item.owner_id == user.id, Item.version == data.expected_version)
        .values(**values, version=next_version, updated_at=utcnow())
    )
    if result.rowcount != 1:
        db.rollback()
        raise _error(409, "stale_version", "This item changed elsewhere. Refresh it before trying again.")
    item = db.query(Item).filter_by(id=item_id, owner_id=user.id).one()
    action = state.lower() if state else "edited"
    item_events.record_item_event(db, owner_id=user.id, item_id=item.id, event_type="act", payload={"action": action})
    db.commit()
    cache.invalidate_user_caches(user.id)
    return _serialize(db, item, user.id)


@router.delete("/{item_id}")
def delete_item(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.query(Item).filter_by(id=item_id, owner_id=user.id).first()
    if item is None:
        raise _error(404, "not_found", "Item not found.")
    item_events.record_item_event(db, owner_id=user.id, item_id=item.id, event_type="dismiss",
                                  payload={"reason": "deleted"})
    db.flush()
    db.query(ItemEvent).filter(ItemEvent.item_id == item.id).update({"item_id": None}, synchronize_session=False)
    db.query(ItemSource).filter_by(item_id=item.id).delete(synchronize_session=False)
    db.delete(item)
    db.commit()
    cache.invalidate_user_caches(user.id)
    return {"ok": True, "id": item_id}
