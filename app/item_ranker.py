"""Cold-start rules-only ranking over item structure; message text is never a scoring input."""
import math
from datetime import datetime, timezone

from app.models import as_utc

ALPHA = 0.70
URGENCY_K = 1.0
EFFORT_MINUTES = {"commitment": 30.0, "follow_up": 15.0, "meeting": 15.0}
EFFORT_BUFFER_MINUTES = 15.0
SENDER_TAU_HOURS = {"slack_dm": 8.0, "email": 24.0, "channel": 48.0, "default": 24.0}
MAX_UNBLOCK_VALUE = 10.0
EFFORT_SIGMA_MINUTES = 60.0
EFFORT_FLOOR = 0.5
UNBLOCK_WEIGHT = 0.5


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 700))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -700))
    return z / (1.0 + z)


def urgency_score(*, now: datetime, due_at: datetime | None, effort_minutes: float,
                  tau_hours: float = SENDER_TAU_HOURS["default"]) -> float:
    """C1-continuous deadline urgency with a bounded overdue branch."""
    now = as_utc(now) or datetime.now(timezone.utc)
    tau_hours = max(float(tau_hours), 1.0)
    effort_hours = max(float(effort_minutes), 0.0) / 60.0
    buffer_hours = EFFORT_BUFFER_MINUTES / 60.0
    deadline = as_utc(due_at)
    hours_left = (deadline - now).total_seconds() / 3600 if deadline else tau_hours
    slack = effort_hours + buffer_hours

    if hours_left >= slack:
        if hours_left <= 0:
            return 1.0
        return _sigmoid(URGENCY_K * (slack - hours_left) / hours_left)

    gamma = URGENCY_K * tau_hours / 4.0
    overdue_ramp = 0.5 + gamma * math.log1p(max(slack - hours_left, 0.0) / tau_hours)
    return min(1.0, max(0.0, overdue_ramp))


def rank_item(*, kind: str, direction: str, state: str, due_at: datetime | None,
              source_structures: list[dict] | None = None, now: datetime | None = None,
              confirmed_project: bool = False, unblock_count: float = 0.0) -> dict:
    """Return rules-only score and an auditable explanation for a confirmed item."""
    now = now or datetime.now(timezone.utc)
    structures = source_structures or []
    effort = EFFORT_MINUTES.get(kind, EFFORT_MINUTES["commitment"])
    tau = SENDER_TAU_HOURS["default"]
    value = 0.55
    reasons = ["confirmed item"]

    if direction in ("asked_of_me", "i_promised"):
        value += 0.18
        reasons.append("you own the next step")
    elif direction == "they_promised":
        value += 0.08
        reasons.append("another person owes you an update")
    if kind == "meeting":
        value += 0.08
        reasons.append("meeting preparation")
    if structures:
        if any(s.get("mentions_me") for s in structures):
            value += 0.12
            reasons.append("directly addressed to you")
        if any(s.get("is_dm") for s in structures):
            value += 0.06
            reasons.append("direct message")
        providers = {s.get("provider") for s in structures}
        tau = SENDER_TAU_HOURS["email"] if "gmail" in providers else (
            SENDER_TAU_HOURS["slack_dm"] if any(s.get("is_dm") for s in structures)
            else SENDER_TAU_HOURS["channel"])
    value = min(1.0, max(0.0, value))
    urgency = urgency_score(now=now, due_at=due_at, effort_minutes=effort, tau_hours=tau)
    base = ALPHA * value + (1 - ALPHA) * urgency

    project_weight = 1.0 if confirmed_project else 0.0
    effective_unblock = min(max(float(unblock_count), 0.0), MAX_UNBLOCK_VALUE) * project_weight
    effort_weight = EFFORT_FLOOR + (1 - EFFORT_FLOOR) * math.exp(-effort / EFFORT_SIGMA_MINUTES)
    final_factor = max(effort_weight, 1 - effort_weight + (effective_unblock / MAX_UNBLOCK_VALUE) * UNBLOCK_WEIGHT)
    score = min(1.0, max(0.0, base * final_factor))

    if due_at:
        reasons.append("deadline urgency")
    if state == "WAITING_ON_OTHER":
        score *= 0.65
        reasons.append("waiting on someone else")
    return {
        "score": round(score, 6),
        "value": round(value, 4),
        "urgency": round(urgency, 4),
        "effort_weight": round(effort_weight, 4),
        "explanation": "Ranked for " + ", ".join(reasons[:3]) + ".",
    }
