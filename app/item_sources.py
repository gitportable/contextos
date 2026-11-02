"""Maintain exact item provenance when source messages are removed."""
import json

from sqlalchemy.orm import Session

from app.models import Item, ItemSource


def detach_message_sources(db: Session, message_ids: list[int]) -> set[int]:
    if not message_ids:
        return set()
    item_ids = {
        item_id for (item_id,) in db.query(ItemSource.item_id)
        .filter(ItemSource.message_id.in_(message_ids)).distinct().all()
    }
    db.query(ItemSource).filter(ItemSource.message_id.in_(message_ids)).delete(synchronize_session=False)
    for item_id in item_ids:
        item = db.query(Item).filter_by(id=item_id).first()
        if item is None:
            continue
        remaining = db.query(ItemSource).filter_by(item_id=item_id).all()
        item.source_message_ids = json.dumps(sorted(source.message_id for source in remaining), separators=(",", ":"))
        connection_ids = {source.connection_id for source in remaining}
        item.connection_id = next(iter(connection_ids)) if len(connection_ids) == 1 else None
    return item_ids
