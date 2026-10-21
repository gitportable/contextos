"""Demo data that flows through the REAL pipeline (never inserted into tables directly).

`build_events` fabricates ~600 Slack Events API envelopes over 14 days: five projects discussed in channels, DMs and a
group DM, thread replies (some without the project name, so they must inherit it), bot alerts, deadline requests, meeting
asks, FYIs, chatter, your own outstanding asks, edits, deletes and replayed duplicates. They are fed to the same
accept_event() the HTTP endpoint uses; the worker does the rest. Seeded with a fixed random seed: identical every run.
CLI: python -m app.demo --email you@example.com [--reset]
"""
import argparse
import random
import time

from sqlalchemy.orm import Session

from app.cache import invalidate_user_caches
from app.config import settings
from app.models import (EventReceipt, OutboundAction, PipelineEvent, ProcessingJob, Project, SlackConnection, SlackConversation, SlackMessage,
                        SlackUser, Summary, User, utcnow)
from app.pipeline import purge_connection_data
from app.slack_events import accept_event

ME = "UDEMOYOU"
PEOPLE = {ME: "You (demo)", "UDEMOPRIYA": "Priya Nair", "UDEMOMARCUS": "Marcus Chen", "UDEMOANA": "Ana Ruiz", "UDEMODEV": "Dev Patel",
          "UDEMOSARA": "Sara Okafor", "UDEMOLEO": "Leo Fischer", "UDEMOMIA": "Mia Tanaka"}
OTHERS = [u for u in PEOPLE if u != ME]
CHANNELS = {  # id -> (name, conversation type, channel_type used in events)
    "CDEMOINC": ("eng-incidents", "public_channel", "channel"), "CDEMOATLAS": ("proj-atlas", "public_channel", "channel"),
    "CDEMOPROD": ("product", "public_channel", "channel"), "CDEMODESIGN": ("design", "public_channel", "channel"),
    "GDEMOLEAD": ("leadership", "private_channel", "group"), "DDEMOPRIYA": ("DM: Priya Nair", "im", "im"),
    "DDEMOMARCUS": ("DM: Marcus Chen", "im", "im"), "GDEMOMPIM": ("mpim-you-marcus-ana", "mpim", "mpim")}
# project -> (surface forms used in messages, documents, channels where it is discussed)
PROJECTS = {
    "Atlas": (["Project Atlas", "Atlas migration", "Atlas"], ["cutover runbook", "ledger schema", "rollback plan"], ["CDEMOATLAS", "CDEMOINC", "DDEMOMARCUS"]),
    "Orion": (["Orion launch", "Project Orion", "Orion"], ["launch checklist", "app store listing", "press kit"], ["CDEMOPROD", "GDEMOLEAD", "DDEMOPRIYA"]),
    "Helios": (["Helios revamp", "Project Helios", "Helios"], ["dashboard mockups", "data model", "metrics spec"], ["CDEMOPROD", "CDEMODESIGN"]),
    "Nova": (["Nova redesign", "Project Nova", "Nova"], ["onboarding flow", "usability report", "copy deck"], ["CDEMODESIGN", "DDEMOPRIYA"]),
    "Pegasus": (["Pegasus audit", "Project Pegasus", "Pegasus"], ["pen-test findings", "remediation plan", "scope document"], ["GDEMOLEAD", "CDEMOINC", "GDEMOMPIM"])}
DEADLINES = ["by 5pm today", "by tomorrow", "by Friday", "EOD", "by 3pm", "ASAP"]
CHATTER = ["lunch?", "thanks!", "happy friday everyone", "anyone seen the wifi password", "+1", "congrats team", "running 5 min late", "good morning"]
REPLIES = ["Looks good to me.", "On it, will update soon.", "Done, see the shared drive.", "Can we discuss in the standup?", "Agreed.", "Blocked on legal, will chase.", "Thanks, merging now."]
BOT = ["[FIRING] checkout-api p99 latency above 2s for 5 min", "[RESOLVED] checkout-api p99 latency back under threshold", "[MERGED] PR #{n} merged to main",
       "[DEPLOY] web-frontend v2.{n} deployed to production"]


def demo_team(user_id: int) -> str:
    return f"TDEMO{user_id:04d}"


def build_events(user_id: int, now: float, seed: int = 7, n_top: int = 420) -> list[dict]:
    rnd, team, tick = random.Random(seed), demo_team(user_id), [0]

    def ts(minutes_ago: float) -> str:
        tick[0] += 1
        return f"{now - minutes_ago * 60 + tick[0] * 0.000137:.6f}"

    def msg(ch, user, text, ago, **extra):
        e = {"type": "message", "channel": ch, "channel_type": CHANNELS[ch][2], "user": user, "text": text, "ts": ts(ago), **extra}
        return e

    def project_text(p):
        forms, docs, _ = PROJECTS[p]
        s, d = rnd.choice(forms), rnd.choice(docs)
        kind = rnd.choices(["status", "request", "blocker", "decision", "meeting", "fyi", "mention"], [28, 22, 10, 8, 8, 12, 12])[0]
        dl = rnd.choice(DEADLINES)
        return {
            "status": rnd.choice([f"{s} update: the {d} is on track.", f"{s}: finished the {d}, moving to review.", f"Quick status on {s} - {d} drafted, waiting on feedback."]),
            "request": rnd.choice([f"<@{ME}> can you review the {s} {d} {dl}?", f"Need your approval on the {s} {d} {dl}.", f"Please send the {s} {d} to finance {dl}.",
                                   f"Could you confirm the {d} for {s} {dl}?"]),
            "blocker": rnd.choice([f"{s} is blocked on the {d}. Can you unblock?", f"Blocker on {s}: the {d} still needs sign-off."]),
            "decision": rnd.choice([f"Decision: we're shipping the {d} first for {s}.", f"{s} sign-off: approved the {d}."]),
            "meeting": rnd.choice([f"Let's schedule a call about {s} tomorrow at 3pm.", f"Can we meet later this week to go over the {d}? ({s})", f"Let's sync on {s} at 11am."]),
            "fyi": f"FYI the {s} {d} is in the shared drive.",
            "mention": f"<@{ME}> thoughts on the {s} {d}?"}[kind]

    items: list[tuple[float, dict]] = []   # (minutes_ago, envelope-event)
    created: list[dict] = []
    for _ in range(n_top):
        ago = min(14 * 1440 - 30, int(rnd.expovariate(1 / 1500)) + rnd.random())
        roll = rnd.random()
        if roll < 0.74:
            p = rnd.choices(list(PROJECTS), [30, 26, 18, 14, 12])[0]
            ch = rnd.choice(PROJECTS[p][2])
            e = msg(ch, rnd.choice(OTHERS), project_text(p), ago)
        elif roll < 0.90:
            e = msg(rnd.choice(list(CHANNELS)), rnd.choice(OTHERS), rnd.choice(CHATTER), ago)
        else:
            e = msg("CDEMOINC", None, rnd.choice(BOT).format(n=rnd.randint(100, 999)), ago, subtype="bot_message", bot_id="BDEMOBOT", username=rnd.choice(["PagerDuty", "GitHub", "Deploybot"]))
        items.append((ago, e)); created.append(e)
    for _ in range(30):  # your own asks: outstanding "waiting on others" unless someone replies
        p = rnd.choice(list(PROJECTS)); ago = rnd.uniform(60, 3 * 1440)
        e = msg(rnd.choice([c for c in PROJECTS[p][2]]), ME, f"Can you send the {rnd.choice(PROJECTS[p][0])} {rnd.choice(PROJECTS[p][1])} {rnd.choice(DEADLINES)}?", ago)
        items.append((ago, e)); created.append(e)
    for parent in rnd.sample([c for c in created if c.get("user") and c["user"] != ME], 90):  # thread replies (many never name the project)
        for k in range(rnd.randint(1, 3)):
            ago_r = max(1.0, (now - float(parent["ts"])) / 60 - rnd.uniform(3, 90) * (k + 1))
            items.append((ago_r, msg(parent["channel"], rnd.choice(OTHERS), rnd.choice(REPLIES), ago_r, thread_ts=parent["ts"])))
    incident = msg("CDEMOINC", "UDEMOPRIYA", f"<@{ME}> production down: checkout API is returning 500s for every EU customer. Can you confirm the rollback ASAP?", 95)
    items.append((95, incident)); created.append(incident)
    for target in rnd.sample([c for c in created if c.get("user") not in (None, ME) and not c.get("thread_ts")], 4):  # edits
        t_ago = max(1.0, (now - float(target["ts"])) / 60 - 8)
        et = ts(t_ago)
        items.append((t_ago, {"type": "message", "subtype": "message_changed", "channel": target["channel"], "channel_type": target["channel_type"], "ts": et,
                              "message": {"type": "message", "user": target["user"], "ts": target["ts"], "text": target["text"].rstrip(".?") + " (updated: now due Friday)", "edited": {"user": target["user"], "ts": et}},
                              "previous_message": {"ts": target["ts"], "text": target["text"]}}))
    for target in rnd.sample([c for c in created if c.get("user") not in (None, ME) and not c.get("thread_ts")], 3):  # deletes
        t_ago = max(1.0, (now - float(target["ts"])) / 60 - 5)
        items.append((t_ago, {"type": "message", "subtype": "message_deleted", "channel": target["channel"], "channel_type": target["channel_type"], "ts": ts(t_ago),
                              "deleted_ts": target["ts"], "previous_message": {"ts": target["ts"], "text": target["text"]}}))
    items.sort(key=lambda x: -x[0])  # oldest first
    envs = [{"type": "event_callback", "team_id": team, "event_id": f"Ev-DEMO-{user_id}-{i + 1:04d}", "event": ev,
             "authorizations": [{"team_id": team, "user_id": ME, "is_bot": False}]} for i, (_, ev) in enumerate(items)]
    for dup in rnd.sample(envs, 3):  # Slack redelivers: same event_id again
        envs.append(dict(dup))
    return envs


def seed_demo(db: Session, user: User, *, process: bool = True, budget_seconds: float = 25.0) -> dict:
    team = demo_team(user.id)
    conn = db.query(SlackConnection).filter_by(owner_id=user.id, team_id=team).first()
    restoring = conn is not None and conn.status == "disconnected"
    if conn is None:
        scopes = ",".join(sorted({"channels:read", "channels:history", "im:read", "im:history", "groups:read", "groups:history", "mpim:read", "mpim:history", "users:read", "chat:write"}))
        conn = SlackConnection(owner_id=user.id, team_id=team, slack_user_id=ME, team_name="Acme (demo workspace)", is_demo=True, status="connected",
                               granted_user_scopes=scopes, requested_user_scopes=scopes, user_token_encrypted="", last_sync_finished_at=utcnow())
        db.add(conn); db.flush()
    elif restoring:
        conn.status, conn.revoked_at, conn.sync_state, conn.last_sync_error = "connected", None, "idle", None
        if not db.query(SlackMessage.id).filter_by(connection_id=conn.id).first():
            db.query(EventReceipt).filter(EventReceipt.event_id.like(f"Ev-DEMO-{user.id}-%")).delete(synchronize_session=False)
    for uid, name in PEOPLE.items():
        if not db.query(SlackUser).filter_by(team_id=team, user_id=uid).first():
            db.add(SlackUser(team_id=team, user_id=uid, display_name=name))
    for cid, (name, ctype, _) in CHANNELS.items():
        if not db.query(SlackConversation).filter_by(connection_id=conn.id, channel_id=cid).first():
            db.add(SlackConversation(connection_id=conn.id, channel_id=cid, name=name, conv_type=ctype,
                                     peer_user_id={"DDEMOPRIYA": "UDEMOPRIYA", "DDEMOMARCUS": "UDEMOMARCUS"}.get(cid), backfill_complete=True))
    db.commit()
    accepted = duplicates = 0
    envs = build_events(user.id, time.time())
    for ev in envs:
        res = accept_event(db, ev)
        accepted += bool(res.get("queued")); duplicates += bool(res.get("duplicate"))
    processed = 0
    if process:
        from app.worker import run_pending_jobs
        processed = run_pending_jobs(max_seconds=budget_seconds)
    remaining = db.query(ProcessingJob).filter(ProcessingJob.status.in_(("queued", "processing")),
                                               (ProcessingJob.team_id == team) | (ProcessingJob.connection_id == conn.id)).count()
    discovered = None
    if process and remaining == 0:  # everything ingested: run project discovery + summaries now so the demo is complete
        from app.worker import run_discovery, run_pending_jobs as drain
        discovered = run_discovery(db, user.id)
        drain(max_seconds=budget_seconds)
    invalidate_user_caches(user.id)
    return {"connection_id": conn.id, "events_total": len(envs), "events_accepted": accepted, "duplicate_events_dropped": duplicates,
            "jobs_processed_inline": processed, "jobs_remaining": remaining, "projects": discovered,
            "worker_needed": remaining > 0,
            "note": "Events went accept_event -> slack_event job -> fan-out -> slack_process_message -> pipeline -> projects -> summaries."}


def remove_demo(db: Session, user: User) -> dict:
    team = demo_team(user.id)
    conn = db.query(SlackConnection).filter_by(owner_id=user.id, team_id=team, is_demo=True).first()
    removed = 0
    if conn:
        removed = purge_connection_data(db, conn)
        db.query(OutboundAction).filter(OutboundAction.connection_id == conn.id).delete(synchronize_session=False)
        db.query(ProcessingJob).filter(ProcessingJob.connection_id == conn.id).delete(synchronize_session=False)
        db.delete(conn)
    db.query(ProcessingJob).filter(ProcessingJob.team_id == team).delete(synchronize_session=False)
    db.query(EventReceipt).filter(EventReceipt.event_id.like(f"Ev-DEMO-{user.id}-%")).delete(synchronize_session=False)
    db.query(SlackUser).filter(SlackUser.team_id == team).delete(synchronize_session=False)
    if not db.query(SlackConnection).filter_by(owner_id=user.id).first():  # no real data left: drop derived project data too
        db.query(Summary).filter_by(owner_id=user.id).delete(synchronize_session=False)
        db.query(Project).filter_by(owner_id=user.id).delete(synchronize_session=False)
    db.query(PipelineEvent).filter((PipelineEvent.team_id == team) | (PipelineEvent.owner_id == user.id)).delete(synchronize_session=False)
    db.commit()
    invalidate_user_caches(user.id)
    from app.projects import invalidate
    invalidate(user.id)
    return {"removed_messages": removed}


def main() -> None:
    from app.database import SessionLocal, init_db
    parser = argparse.ArgumentParser(description="Seed demo Slack data through the real pipeline")
    parser.add_argument("--email", required=True)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        user = db.query(User).filter_by(email=args.email.strip().lower()).first()
        if not user:
            raise SystemExit(f"No user {args.email}. Sign in once in the UI first.")
        print(remove_demo(db, user) if args.reset else seed_demo(db, user, budget_seconds=600))


if __name__ == "__main__":
    main()
