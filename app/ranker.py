"""Prioritisation, rules-only mode of Algorithm v7.4.1 (section 7.2, the free-tier default).

  * Rules GATE (who may be urgent at all: directed at you, not a bot, recent, not handled). They live in triage.py.
  * A small, interpretable weighted sum RANKS what passes: sender, request, deadline urgency, recency.
  * Deadline urgency is the spec's C1-continuous curve:  U = sigmoid(k(x))  for x <= 0,  0.5 + g*ln(1 + x/tau)  for x > 0,
    with x = e + b - hours_left and g = k*tau/4, so the curve has no kink where it changes shape.
  * The user's labels (Not urgent / Wrong person / Not mine / Always show) move the sender weight from day one.
  * Every score comes with ONE plain sentence saying why, built from the strongest signals. No LLM is involved in ranking.
"""
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.priority import (BLOCKER_RE, DEADLINE_RE, DECISION_RE, FYI_RE, NEGATED_URGENCY_RE, P0_RE, REQUEST_RE, extract_due, get_tz)

WEIGHTS = {"sender": 0.25, "request": 0.30, "deadline": 0.30, "recency": 0.15}
K, TAU, EFFORT_H, BUFFER_H = 0.5, 6.0, 0.25, 1.0   # urgency steepness, soft tail scale, assumed effort, safety buffer (hours)
GAMMA = K * TAU / 4                                  # C1 continuity at the seam between the two branches
RECENCY_HALF_LIFE_H = 18.0
P1_AT, P2_AT = 55, 30
LIVE_URGENT_AT = 45                                  # a P1 that has aged below this no longer counts as urgent
LABEL_SHIFT = {"not_urgent": -0.15, "wrong_person": -0.25, "not_mine": -0.25, "always_show": 0.5}
LABELS = tuple(LABEL_SHIFT)


def urgency(hours_left: float | None) -> float:
    """0 when there is no deadline; otherwise rises smoothly as the deadline nears and keeps rising once it is tight."""
    if hours_left is None:
        return 0.0
    x = EFFORT_H + BUFFER_H - hours_left
    u = 1 / (1 + math.exp(-K * x)) if x <= 0 else 0.5 + GAMMA * math.log(1 + x / TAU)
    return max(0.0, min(1.0, u))


@dataclass
class Features:
    sender: float
    request: float
    deadline: float
    recency: float
    kind: str              # blocker | approval | request | plain
    hours_left: float | None
    due: datetime | None
    directed: str          # dm | mention | none
    incident: bool
    muted: str             # "" | fyi | bot | negated


def extract(text: str, created_at: datetime | None, now: datetime, *, is_dm: bool, mentions_me: bool, is_bot: bool,
            sender_shift: float = 0.0) -> Features:
    text = (text or "").strip()
    negated = bool(NEGATED_URGENCY_RE.search(text))
    scrubbed = NEGATED_URGENCY_RE.sub(" ", text)
    kind, request = "plain", 0.0
    if BLOCKER_RE.search(scrubbed):
        kind, request = "blocker", 1.0
    elif DECISION_RE.search(scrubbed):
        kind, request = "approval", 0.8
    elif REQUEST_RE.search(scrubbed):
        kind, request = "request", 0.6
    directed = "dm" if is_dm else "mention" if mentions_me else "none"
    sender = {"dm": 0.55, "mention": 0.65, "none": 0.15}[directed] + sender_shift
    created = created_at if created_at is not None else now
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    hours_left, due = None, None
    if DEADLINE_RE.search(scrubbed):
        due, _ = extract_due(scrubbed, created)
        hours_left = (due - now).total_seconds() / 3600 if due else 4.0   # "ASAP" with no time: treat as a few hours
    age_h = max(0.0, (now - created).total_seconds() / 3600)
    muted = "bot" if is_bot else "fyi" if FYI_RE.search(text) else "negated" if negated else ""
    return Features(max(0.0, min(1.0, sender)), request, urgency(hours_left), 0.5 ** (age_h / RECENCY_HALF_LIFE_H),
                    kind, hours_left, due, directed, bool(P0_RE.search(scrubbed) and BLOCKER_RE.search(scrubbed)), muted)


def score(f: Features) -> tuple[int, str]:
    total = 100 * (WEIGHTS["sender"] * f.sender + WEIGHTS["request"] * f.request + WEIGHTS["deadline"] * f.deadline + WEIGHTS["recency"] * f.recency)
    if f.muted == "bot":
        total *= 0.2
    elif f.muted == "fyi":
        total *= 0.5
    elif f.muted == "negated":
        total *= 0.6
    if f.incident:
        return max(90, int(round(total))), "P0"
    s = max(0, min(100, int(round(total))))
    return s, "P1" if s >= P1_AT else "P2" if s >= P2_AT else "P3"


def _when(due: datetime | None, hours_left: float | None) -> str:
    if due is None:
        return "soon" if hours_left is not None else ""
    local = due.astimezone(get_tz())
    today = datetime.now(get_tz()).date()
    clock = local.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    day = "today" if local.date() == today else "tomorrow" if (local.date() - today).days == 1 else local.strftime("%A")
    return f"{clock} {day}"


def explain(f: Features, author: str | None = None) -> str:
    """One plain sentence a non-technical person can check against the message."""
    who = (author or "").strip().split(" ")[0] or "Someone"
    if f.incident:
        return f"{who} reports a critical outage, so this is treated as urgent whatever else is going on."
    if f.muted == "bot":
        return "This is an automated message, so it can wait."
    if f.muted == "fyi":
        return f"{who} marked this as FYI, so nothing is needed from you."
    action = {"blocker": "says something is blocked", "approval": "needs your approval", "request": "asked you for something"}.get(f.kind, "")
    to_you = {"dm": "wrote to you directly", "mention": "mentioned you"}.get(f.directed, "")
    when = _when(f.due, f.hours_left)
    if f.hours_left is not None and f.hours_left < 0:
        when_txt = ", and the deadline it mentioned has passed"
    elif when:
        when_txt = f", by {when}" if when != "soon" else ", and it sounds time-sensitive"
    else:
        when_txt = ""
    if action:
        tail = f", and {to_you}" if to_you and f.directed else ""
        return f"{who} {action}{when_txt}{tail}."
    if to_you:
        return f"{who} {to_you}{when_txt}, but there's no clear request, so it isn't urgent."
    return f"This is general conversation with nothing aimed at you, so it can wait."


def rank_message(text: str, created_at: datetime | None, author: str | None, *, is_dm: bool, mentions_me: bool, is_bot: bool,
                 sender_shift: float = 0.0, now: datetime | None = None) -> tuple[int, str, str]:
    now = now or datetime.now(timezone.utc)
    f = extract(text, created_at, now, is_dm=is_dm, mentions_me=mentions_me, is_bot=is_bot, sender_shift=sender_shift)
    s, label = score(f)
    return s, label, explain(f, author)


def live_score(row, now: datetime, shifts: dict[str, float]) -> int:
    """Score right now (the stored one is frozen at ingestion): deadlines get closer, old things fade, labels count."""
    f = extract(row.text, row.message_created_at or row.ingested_at, now, is_dm=(row.conv_type == "im"), mentions_me=bool(row.mentions_me),
                is_bot=bool(row.is_bot), sender_shift=shifts.get(sender_key(row), 0.0))
    return score(f)[0]


def sender_key(row) -> str:
    return f"{row.connection_id}:{row.user_id or (row.author_name or '').lower()}"


def sender_shifts(db, owner_id: int) -> dict[str, float]:
    """Learned sender weight from the user's own labels: each 'Not urgent' lowers a sender a little, 'Wrong person' / 'Not mine'
    more, 'Always show' raises them. Bounded so no sender is ever silenced or promoted forever by a few clicks."""
    from sqlalchemy import func

    from app.models import MessageFeedback
    out: dict[str, float] = {}
    for key, label, n in (db.query(MessageFeedback.sender_key, MessageFeedback.label, func.count(MessageFeedback.id))
                          .filter(MessageFeedback.owner_id == owner_id).group_by(MessageFeedback.sender_key, MessageFeedback.label).all()):
        out[key] = out.get(key, 0.0) + LABEL_SHIFT.get(label, 0.0) * n
    return {k: max(-0.6, min(0.6, v)) for k, v in out.items()}
