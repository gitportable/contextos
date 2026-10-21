"""Deterministic priority scoring and date extraction (no LLM key required).

Improvements over the MVP version:
  * context-aware: mentions of you, DMs and @channel broadcasts change the score
  * negation ("not urgent") does not trigger urgency
  * due dates are computed in APP_TIMEZONE (not UTC) and understand clock times and weekdays
"""
import re
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from app.config import settings

DEADLINE_RE = re.compile(r"\b(today|tonight|tomorrow|eod|end of day|asap|urgent|by \d{1,2}(?::\d{2})?\s?(?:am|pm)?)\b", re.I)
REQUEST_RE = re.compile(r"\b(can you|could you|would you|please|need you to|action required|please review|let me know|follow up|send|approve|confirm|reply)\b", re.I)
BLOCKER_RE = re.compile(r"\b(blocked|blocker|blocking|can't proceed|cannot proceed|production down|outage|critical|sev[ -]?1)\b", re.I)
DECISION_RE = re.compile(r"\b(approve|approval|decision|sign off|sign-off)\b", re.I)
FYI_RE = re.compile(r"^\s*(fyi|for your information)\b", re.I)
NEGATED_URGENCY_RE = re.compile(r"\b(?:not|no|isn't|is not|never|without)\s+(?:an?\s+)?(?:urgent|asap|critical|emergency)\b", re.I)
P0_RE = re.compile(r"\b(production down|sev[ -]?1|critical outage)\b", re.I)

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
WEEKDAY_RE = re.compile(r"\b(?:by|before|on|until|this|next|every)\s+(" + "|".join(WEEKDAYS) + r")\b", re.I)
CLOCK_RE = re.compile(r"\b(?:by|at|before|until|@)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
MEETING_RE = re.compile(r"\b(meet|meeting|sync|schedule|calendar|call|huddle|catch[ -]?up|let'?s talk|standup|stand-up|1:1|one-on-one)\b", re.I)
INVITE_DATE_RE = re.compile(
    r"\b(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)[, ]+)?"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
    re.I,
)
TIME_RANGE_RE = re.compile(
    r"\b(?P<start>\d{1,2})(?::(?P<start_min>\d{2}))?\s*(?P<start_meridiem>am|pm)?"
    r"\s*(?:-|–|—|to)\s*"
    r"(?P<end>\d{1,2})(?::(?P<end_min>\d{2}))?\s*(?P<end_meridiem>am|pm)\b",
    re.I,
)


def get_tz(name: str | None = None):
    name = name or settings.app_timezone
    try:
        if ZoneInfo:
            return ZoneInfo(name)
    except Exception:
        pass
    return timezone.utc  # tzdata missing (common on Windows): `pip install tzdata`


def score_message(text: str, created_at=None, *, is_dm: bool = False, mentions_me: bool = False,
                  broadcast: bool = False, is_bot: bool = False):
    normalized = (text or "").strip()
    score, reasons = 10, []
    negated = bool(NEGATED_URGENCY_RE.search(normalized))
    scrubbed = NEGATED_URGENCY_RE.sub(" ", normalized)
    if REQUEST_RE.search(scrubbed):
        score += 20; reasons.append("direct request/action language")
    if DEADLINE_RE.search(scrubbed):
        score += 25; reasons.append("deadline/urgency language")
    if BLOCKER_RE.search(scrubbed):
        score += 25; reasons.append("blocker/incident language")
    if DECISION_RE.search(scrubbed):
        score += 10; reasons.append("decision or approval needed")
    if MEETING_RE.search(scrubbed):
        score += 30; reasons.append("meeting or scheduled-time language")
    if MEETING_RE.search(scrubbed) and INVITE_DATE_RE.search(scrubbed) and TIME_RANGE_RE.search(scrubbed):
        score += 35; reasons.append("dated calendar invitation")
    if mentions_me:
        score += 15; reasons.append("mentions you directly")
    if is_dm:
        score += 10; reasons.append("direct message to you")
    if broadcast:
        score += 5; reasons.append("@channel/@here broadcast")
    if FYI_RE.search(normalized):
        score -= 10; reasons.append("FYI phrasing")
    if negated:
        reasons.append("explicitly not urgent")
    if is_bot:
        score -= 5; reasons.append("automated message")
    if len(normalized) < 8:
        score -= 10; reasons.append("very short message")
    score = max(0, min(100, score))
    if P0_RE.search(scrubbed) and BLOCKER_RE.search(scrubbed):
        return max(score, 90), "P0", "; ".join(reasons + ["explicit critical incident signal"])
    label = "P1" if score >= 65 else "P2" if score >= 35 else "P3"
    return score, label, "; ".join(reasons) if reasons else "no strong action signals; baseline priority"


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
    low = (text or "").lower()
    local_now = now.astimezone(tz)
    date_match = INVITE_DATE_RE.search(text or "")
    range_match = TIME_RANGE_RE.search(text or "")
    if date_match and range_match:
        try:
            month = datetime.strptime(date_match.group(1)[:3].title(), "%b").month
            day = int(date_match.group(2))
            year = int(date_match.group(3) or local_now.year)
            event_day = date(year, month, day)
            start_hour = int(range_match.group("start"))
            start_minute = int(range_match.group("start_min") or 0)
            meridiem = (range_match.group("start_meridiem") or range_match.group("end_meridiem")).lower()
            if not 1 <= start_hour <= 12 or start_minute > 59:
                raise ValueError("Invalid meeting time")
            if meridiem == "pm" and start_hour < 12:
                start_hour += 12
            elif meridiem == "am" and start_hour == 12:
                start_hour = 0
            start = datetime.combine(event_day, time(start_hour, start_minute), tzinfo=tz)
            return title, start.astimezone(timezone.utc), False
        except ValueError:
            pass
    clock_match = CLOCK_RE.search(low)
    if clock_match and not clock_match.group(3):
        hour, minute = int(clock_match.group(1)), int(clock_match.group(2) or 0)
        if minute <= 59 and hour <= 12:
            local_now = now.astimezone(tz)
            day, _ = _target_day(low, local_now.date())
            candidate_day = day or local_now.date()
            hours = [hour]
            if 1 <= hour < 12:
                hours.append(hour + 12)
            candidates = [datetime.combine(candidate_day, time(h, minute), tzinfo=tz) for h in hours]
            start = min((candidate for candidate in candidates if candidate > local_now), default=None)
            if start is None:
                start = datetime.combine(candidate_day + timedelta(days=1), time(min(hours), minute), tzinfo=tz)
            return title, start.astimezone(timezone.utc), True
    start, explicit = extract_due(text, now, tz, default_hours={"today": 14, "tomorrow": 14, "weekday": 14})
    if start is not None:
        return title, start, not explicit
    local = now.astimezone(tz) + timedelta(days=1)
    return title, datetime.combine(local.date(), time(14, 0), tzinfo=tz).astimezone(timezone.utc), True
