"""Structural priority scoring and legacy text parsing.

Ranking deliberately ignores message text. The remaining regexes support existing assistant/date behavior
until the item-extraction stage replaces those paths with confidence-aware structured extraction.
"""
import re
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from app.config import settings

REQUEST_RE = re.compile(r"\b(can you|could you|would you|please|need you to|action required|please review|let me know|follow up|send|approve|confirm|reply)\b", re.I)
DEADLINE_RE = re.compile(r"\b(today|tonight|tomorrow|eod|end of day|asap|urgent|by \d{1,2}(?::\d{2})?\s?(?:am|pm)?)\b", re.I)
BLOCKER_RE = re.compile(r"\b(blocked|blocker|blocking|can't proceed|cannot proceed|production down|outage|critical|sev[ -]?1)\b", re.I)

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
WEEKDAY_RE = re.compile(r"\b(?:by|before|on|until|this|next|every)\s+(" + "|".join(WEEKDAYS) + r")\b", re.I)
CLOCK_RE = re.compile(r"\b(?:by|at|before|until|@)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
MEETING_RE = re.compile(r"\b(meet|meeting|sync|schedule|calendar|call|huddle|catch[ -]?up|let'?s talk|standup|stand-up|1:1|one-on-one)\b", re.I)


def get_tz(name: str | None = None):
    name = name or settings.app_timezone
    try:
        if ZoneInfo:
            return ZoneInfo(name)
    except Exception:
        pass
    return timezone.utc  # tzdata missing (common on Windows): `pip install tzdata`


def score_structural(*, is_dm: bool = False, mentions_me: bool = False, broadcast: bool = False,
                     is_bot: bool = False, is_self: bool = False, reply_count: int = 0,
                     has_attachment: bool = False):
    """Fixed-weight linear score over source structure, with no text or keyword features."""
    if is_bot:
        return 0, "P3", "automated sender filtered from ranking"
    if is_self:
        return 0, "P3", "outgoing message; commitment extraction is handled separately"

    score, reasons = 10, ["baseline"]
    if mentions_me:
        score += 50
        reasons.append("directly addressed to you")
    if is_dm:
        score += 20
        reasons.append("direct message")
    if has_attachment:
        score += 5
        reasons.append("contains an attachment")
    active_replies = min(max(int(reply_count or 0), 0), 3)
    if active_replies:
        score += active_replies * 2
        reasons.append("active thread")
    if broadcast and not mentions_me:
        score -= 5
        reasons.append("broad-channel broadcast")

    score = max(0, min(100, score))
    label = "P0" if score >= 90 else "P1" if score >= 60 else "P2" if score >= 30 else "P3"
    return score, label, "; ".join(reasons)


def score_message(text: str = "", created_at=None, *, is_dm: bool = False, mentions_me: bool = False,
                  broadcast: bool = False, is_bot: bool = False, is_self: bool = False,
                  reply_count: int = 0, has_attachment: bool = False):
    """Compatibility entry point; `text` and `created_at` are intentionally not scoring inputs."""
    del text, created_at
    return score_structural(is_dm=is_dm, mentions_me=mentions_me, broadcast=broadcast, is_bot=is_bot,
                            is_self=is_self, reply_count=reply_count, has_attachment=has_attachment)


def _parse_clock(low: str):
    m = CLOCK_RE.search(low)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if minute > 59 or hour > 23:
        return None
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif not ampm and 1 <= hour <= 7:
        hour += 12  # "by 5" in a work chat means 17:00
    return time(hour, minute)


def _target_day(low: str, today: date):
    """Return (date, kind) where kind in {'today','tomorrow','weekday'} or (None, None)."""
    if re.search(r"\btomorrow\b", low):
        return today + timedelta(days=1), "tomorrow"
    if re.search(r"\b(today|tonight|eod|end of day)\b", low):
        return today, "today"
    m = WEEKDAY_RE.search(low)
    if m:
        ahead = (WEEKDAYS[m.group(1).lower()] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead), "weekday"
    return None, None


def extract_due(text: str, now=None, tz=None, *, default_hours=None):
    """Return (utc_datetime | None, had_explicit_clock). Pure function; `now` is the message time."""
    now = now or datetime.now(timezone.utc)
    tz = tz or get_tz()
    default_hours = default_hours or {"today": 17, "tomorrow": 9, "weekday": 17}
    low = (text or "").lower()
    local_now = now.astimezone(tz)
    clock = _parse_clock(low)
    day, kind = _target_day(low, local_now.date())
    if day is None:
        if not clock:
            return None, False
        day, kind = local_now.date(), "today"
        if datetime.combine(day, clock, tzinfo=tz) <= local_now:
            day, kind = day + timedelta(days=1), "tomorrow"
    due = datetime.combine(day, clock or time(default_hours[kind], 0), tzinfo=tz)
    if due <= local_now:  # e.g. "EOD" said at 18:30
        due = local_now + timedelta(hours=1)
    return due.astimezone(timezone.utc), clock is not None


def extract_reminder_due(text: str, now=None, tz=None):
    """Small deterministic date extractor; returns an aware UTC datetime or None."""
    due, _ = extract_due(text, now, tz)
    return due


def extract_meeting_suggestion(text: str, now=None, tz=None):
    """Returns (title, start_utc, is_provisional) or None. Never invents a time silently:
    when the message states none, the slot is tomorrow 14:00 local and flagged provisional."""
    if not MEETING_RE.search(text or ""):
        return None
    now = now or datetime.now(timezone.utc)
    tz = tz or get_tz()
    title = (text or "").strip()[:180] or "Meeting suggestion"
    start, explicit = extract_due(text, now, tz, default_hours={"today": 14, "tomorrow": 14, "weekday": 14})
    if start is not None:
        return title, start, not explicit
    local = now.astimezone(tz) + timedelta(days=1)
    return title, datetime.combine(local.date(), time(14, 0), tzinfo=tz).astimezone(timezone.utc), True
