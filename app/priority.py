import re
from datetime import datetime, timezone, timedelta

DEADLINE_RE = re.compile(r"\b(today|tonight|tomorrow|eod|asap|urgent|by \d{1,2}(?::\d{2})?\s?(?:am|pm)?)\b", re.I)
REQUEST_RE = re.compile(r"\b(can you|could you|please|need you to|action required|please review|let me know|follow up|send|approve|confirm|reply)\b", re.I)
BLOCKER_RE = re.compile(r"\b(blocked|blocker|blocking|can't proceed|cannot proceed|production down|outage|critical|sev[ -]?1)\b", re.I)
DECISION_RE = re.compile(r"\b(approve|approval|decision|sign off|sign-off)\b", re.I)
FYI_RE = re.compile(r"^\s*(fyi|for your information)\b", re.I)

def score_message(text: str, created_at=None):
    normalized = (text or "").strip()
    score, reasons = 10, []
    if REQUEST_RE.search(normalized):
        score += 20; reasons.append("direct request/action language")
    if DEADLINE_RE.search(normalized):
        score += 25; reasons.append("deadline/urgency language")
    if BLOCKER_RE.search(normalized):
        score += 25; reasons.append("blocker/incident language")
    if DECISION_RE.search(normalized):
        score += 10; reasons.append("decision or approval needed")
    if FYI_RE.search(normalized):
        score -= 10; reasons.append("FYI phrasing")
    if len(normalized) < 8:
        score -= 10; reasons.append("very short message")
    score = max(0, min(100, score))
    if re.search(r"\b(production down|sev[ -]?1|critical outage)\b", normalized, re.I) and BLOCKER_RE.search(normalized):
        return max(score, 90), "P0", "; ".join(reasons + ["explicit critical incident signal"])
    if score >= 65: label = "P1"
    elif score >= 35: label = "P2"
    else: label = "P3"
    # Critical plus deadline/blocker can be P0; ordinary urgent requests remain P1.
    return score, label, "; ".join(reasons) if reasons else "no strong action signals; baseline priority"

def extract_reminder_due(text: str, now=None):
    """Small deterministic date extractor for MVP; returns UTC datetime or None."""
    now = now or datetime.now(timezone.utc)
    low = text.lower()
    if "tomorrow" in low:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    if "today" in low or "tonight" in low or "eod" in low:
        return now.replace(hour=17, minute=0, second=0, microsecond=0)
    return None

def extract_meeting_suggestion(text: str, now=None):
    low = text.lower()
    if not any(x in low for x in ("meet", "meeting", "sync", "schedule", "calendar", "call", "let's talk", "lets talk")):
        return None
    title = text.strip()[:180] or "Meeting suggestion"
    now = now or datetime.now(timezone.utc)
    # Do not guess an actual requested date/time: create a clearly provisional slot.
    start = (now + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
    return title, start
