"""Pure helpers for Slack mrkdwn text and timestamps. No I/O, easy to unit-test."""
import html
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>")
_CHANNEL = re.compile(r"<#([CGD][A-Z0-9]+)(?:\|([^>]*))?>")
_SPECIAL = re.compile(r"<!([a-z]+)(?:\^[^|>]*)?(?:\|([^>]*))?>")
_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]*))?>")
_SUBTEAM = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")
BROADCASTS = {"channel", "here", "everyone"}


def mentioned_user_ids(raw: str) -> set[str]:
    return {m.group(1) for m in _MENTION.finditer(raw or "")}


def mentions_user(raw: str, slack_user_id: str) -> bool:
    return bool(slack_user_id) and slack_user_id in mentioned_user_ids(raw)


def _special(m: re.Match) -> str:
    kind, label = m.group(1), m.group(2)
    if kind in BROADCASTS:
        return "@" + kind
    return label or ""  # <!date^...|fallback> and friends: keep the human-readable fallback


def has_broadcast(raw: str) -> bool:
    return any(m.group(1) in BROADCASTS for m in _SPECIAL.finditer(raw or ""))


def normalize_text(raw: str, names: dict[str, str] | None = None) -> str:
    """Slack mrkdwn -> plain text suitable for scoring, search and embedding."""
    names = names or {}
    text = raw or ""
    text = _SUBTEAM.sub(lambda m: m.group(1) or "@group", text)
    text = _MENTION.sub(lambda m: "@" + (m.group(2) or names.get(m.group(1)) or m.group(1)), text)
    text = _CHANNEL.sub(lambda m: "#" + (m.group(2) or m.group(1)), text)
    text = _SPECIAL.sub(_special, text)
    text = _LINK.sub(lambda m: m.group(2) or m.group(1), text)
    text = html.unescape(text)  # Slack escapes only & < >
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"[ \t]+", " ", text).strip()


def ts_to_decimal(ts: str | None) -> Decimal:
    try:
        return Decimal(str(ts))
    except (InvalidOperation, TypeError):
        return Decimal(0)


def ts_max(*values: str | None) -> str | None:
    vals = [v for v in values if v]
    return max(vals, key=ts_to_decimal) if vals else None


def ts_min(*values: str | None) -> str | None:
    vals = [v for v in values if v]
    return min(vals, key=ts_to_decimal) if vals else None


def ts_to_datetime(ts: str | None) -> datetime:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(timezone.utc)


def datetime_to_ts(dt: datetime) -> str:
    return f"{dt.timestamp():.6f}"


def conv_type_from_channel_type(channel_type: str | None, channel_id: str | None = None) -> str:
    mapping = {"channel": "public_channel", "group": "private_channel", "im": "im", "mpim": "mpim"}
    if channel_type in mapping:
        return mapping[channel_type]
    prefix = (channel_id or "")[:1]
    return {"D": "im", "G": "private_channel"}.get(prefix, "public_channel")
