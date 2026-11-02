"""Persist user feedback as ranking labels without copying message content into the event log."""
import json

from sqlalchemy.orm import Session

from app.models import ItemEvent

MESSAGE_EVENT_TYPES = {
    "open",
    "dwell",
    "act",
    "snooze",
    "dismiss",
    "external_handled",
    "feedback",
}
PAYLOAD_KEYS = {
    "open": set(),
    "dwell": {"duration_ms"},
    "act": {"action"},
    "snooze": {"hours"},
    "dismiss": {"reason"},
    "external_handled": {"source"},
    "feedback": {"label"},
}
FEEDBACK_LABELS = {"not_urgent", "wrong_person", "not_mine", "always_show"}


def _validated_payload(event_type: str, payload: dict | None) -> dict:
    if event_type not in MESSAGE_EVENT_TYPES:
        raise ValueError(f"Unsupported item event type: {event_type}")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict) or set(payload) - PAYLOAD_KEYS[event_type]:
        raise ValueError(f"Invalid payload for item event type: {event_type}")
    if event_type == "feedback" and payload.get("label") not in FEEDBACK_LABELS:
        raise ValueError("Unsupported ranking feedback label")
    return payload


def record_message_event(db: Session, *, owner_id: int, message_id: int, event_type: str,
                         payload: dict | None = None) -> ItemEvent:
    payload = _validated_payload(event_type, payload)
    event = ItemEvent(
        owner_id=owner_id,
        message_id=message_id,
        event_type=event_type,
        payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )
    db.add(event)
    return event


def record_item_event(db: Session, *, owner_id: int, item_id: int, event_type: str,
                      payload: dict | None = None) -> ItemEvent:
    payload = _validated_payload(event_type, payload)
    event = ItemEvent(
        owner_id=owner_id,
        item_id=item_id,
        event_type=event_type,
        payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )
    db.add(event)
    return event
