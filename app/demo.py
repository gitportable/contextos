"""Demo data that flows through the REAL pipeline (never inserted into the message tables directly).

`seed_demo` builds a believable two-week workspace as Slack Events API envelopes and feeds them to accept_event(), the
function the HTTP endpoint uses. The worker then fans them out, scores them, extracts reminders / meetings, links them to
projects, indexes them and summarises each project: every layer the pipeline page shows.

size="full"  ~400 events: 9 people, 10 conversations, 14 days, 5 projects (Phoenix migration, Atlas pricing, checkout-api,
             Q4 roadmap, Orion onboarding), threads, bot alerts, chatter, edits, deletes, replayed events, unanswered DMs.
size="small" ~20 events (the quick smoke test).
The generator is seeded per user, so the same user always gets the same data.

CLI:  python -m app.demo --email you@example.com [--size full|small] [--reset]
"""
import argparse
import random
import time

from sqlalchemy.orm import Session

from app.cache import invalidate_user_caches
from app.config import ACCESS_LEVELS
from app.models import (EventReceipt, OutboundAction, PipelineEvent, ProcessingJob, SlackConnection, SlackConversation, SlackUser, User, utcnow)
from app.pipeline import purge_connection_data
from app.slack_events import accept_event

ME = "UDEMOYOU"
PEOPLE = {ME: "You (demo)", "UDEMOPRIYA": "Priya Nair", "UDEMOMARCUS": "Marcus Chen", "UDEMOANA": "Ana Ruiz", "UDEMODEV": "Dev Patel",
          "UDEMOLENA": "Lena Fischer", "UDEMOOMAR": "Omar Haddad", "UDEMOSOFIA": "Sofia Rossi", "UDEMOKENJI": "Kenji Tanaka"}
CHANNELS = {  # id -> (name, type, channel_type used in events)
    "CDEMOINC": ("eng-incidents", "public_channel", "channel"),
    "CDEMOPLAT": ("eng-platform", "public_channel", "channel"),
    "CDEMOPROD": ("product", "public_channel", "channel"),
    "CDEMODSGN": ("design", "public_channel", "channel"),
    "CDEMOMKT": ("marketing", "public_channel", "channel"),
    "CDEMORAND": ("random", "public_channel", "channel"),
    "GDEMOLEAD": ("leadership", "private_channel", "group"),
    "DDEMOPRIYA": ("DM: Priya Nair", "im", "im"),
    "DDEMOANA": ("DM: Ana Ruiz", "im", "im"),
    "GDEMOMPIM": ("mpim-you-marcus-ana", "mpim", "mpim"),
}
P, MC, A, D, L, O, S, K = ("UDEMOPRIYA", "UDEMOMARCUS", "UDEMOANA", "UDEMODEV", "UDEMOLENA", "UDEMOOMAR", "UDEMOSOFIA", "UDEMOKENJI")
ME_TAG = f"<@{ME}>"

# (channel, author, text, [thread replies as (author, text)]) per project. "{me}" expands to a mention of the demo user.
PROJECT_BEATS: dict[str, list[tuple]] = {
    "phoenix": [
        ("CDEMOPLAT", P, "Kickoff notes for the Phoenix migration are in the wiki. Please read before Monday's planning.", [(D, "Read it, the rollback section needs an owner."), (P, "Marcus, can you own rollback?")]),
        ("CDEMOPLAT", D, "Phoenix replica is up. Replication lag is ~40 minutes, investigating the WAL settings.", []),
        ("CDEMOINC", MC, "Heads up: Phoenix replication lag spiked again overnight, we are blocked on the cutover until it stays under 5 minutes.", [(D, "Tuning checkpoint settings now."), (MC, "Lag is down to 12 minutes.")]),
        ("CDEMOPLAT", D, "Schema diff for the Phoenix migration looks clean, only two indexes need rebuilding.", []),
        ("GDEMOLEAD", P, "{me} can you approve the Phoenix cutover window by Friday? We need a deploy freeze from 22:00.", [(A, "Fine from product, no launches that night.")]),
        ("CDEMOPLAT", MC, "Dry run of the Phoenix cutover finished: 3 of 12 services failed health checks.", [(D, "Billing and search, both are DNS caching."), (MC, "Fix is in review.")]),
        ("CDEMOPLAT", D, "Phoenix dry run #2 passed for 11 of 12 services. Search still flapping.", []),
        ("CDEMOINC", P, "Reminder: Phoenix cutover rehearsal is tomorrow at 3pm, everyone on the platform rota please join.", []),
        ("CDEMOPLAT", MC, "Phoenix runbook updated with the rollback steps and the DNS TTL change.", []),
        ("GDEMOLEAD", P, "Status on Phoenix migration: two blockers left, replication lag and the search service. Risk is medium.", []),
        ("CDEMOPLAT", D, "Search service fixed for Phoenix, flapping was a stale connection pool.", []),
        ("CDEMOPLAT", K, "Do we need to re-run the analytics exports after the Phoenix migration? Data team wants to know.", [(D, "Yes, one backfill after cutover.")]),
        ("CDEMOINC", MC, "Phoenix cutover scheduled for Saturday 22:00. Confirm you can be on call.", []),
        ("DDEMOPRIYA", P, "Could you confirm you're on call for the Phoenix cutover? I need an answer by tomorrow.", []),
        ("CDEMOPLAT", D, "Phoenix post-migration checklist drafted, please add anything I missed.", []),
        ("CDEMOPLAT", MC, "Phoenix migration retro doc is open for comments.", []),
    ],
    "atlas": [
        ("CDEMOPROD", A, "Draft of the Atlas pricing redesign is ready. Three tiers instead of five.", [(O, "Finance likes fewer tiers."), (S, "Marketing needs the tier names soon.")]),
        ("CDEMOMKT", S, "Atlas pricing page copy v1 is in the doc. Feedback by Wednesday please.", []),
        ("GDEMOLEAD", O, "Margin model for the Atlas pricing redesign: the middle tier is below target at the current discount.", [(A, "We can drop the annual discount to 15%.")]),
        ("CDEMOPROD", A, "{me} please review the Atlas pricing tiers and send me feedback by 5pm today. I need your approval before the board deck.", []),
        ("CDEMODSGN", L, "Atlas pricing page mockups are in Figma, two layouts to compare.", [(S, "Layout B converts better in the test."), (L, "Going with B.")]),
        ("CDEMOMKT", S, "A/B test plan for Atlas pricing: 50/50 split for two weeks, starting Monday.", []),
        ("GDEMOLEAD", A, "Atlas pricing decision needed this week: keep grandfathering for existing customers or not?", [(O, "Grandfather for 12 months."), (P, "Agree.")]),
        ("CDEMOPROD", A, "Customer interviews so far support the Atlas pricing direction. Seven of nine prefer fewer tiers.", []),
        ("CDEMOMKT", S, "Atlas pricing announcement email scheduled for the 28th, needs final tier names.", []),
        ("CDEMOPROD", O, "Billing changes for Atlas pricing need two sprints. Can we cut the usage-based add-on?", [(A, "Cut it from launch, ship later.")]),
        ("DDEMOANA", A, "Can you sign off on the Atlas pricing grandfathering policy by Friday? Legal wants it in writing.", []),
        ("CDEMOMKT", S, "Atlas pricing FAQ drafted, please review the refund wording.", []),
        ("CDEMOPROD", A, "Atlas pricing launch checklist: billing, page, email, support macros. Owners are in the sheet.", []),
        ("GDEMOLEAD", O, "Atlas pricing revenue forecast updated: +6% ARR in the base case.", []),
    ],
    "checkout": [
        ("CDEMOINC", P, "{me} production down: checkout-api is returning 500s for every EU customer. Can you confirm the rollback ASAP?", [(MC, "Rolling back now. I'll post an update in 10 minutes."), (P, "Rollback confirmed, error rate is back to normal.")]),
        ("CDEMOPLAT", D, "Root cause of the checkout-api outage: a bad config flag for the EU payment provider.", []),
        ("CDEMOPLAT", MC, "Adding a canary stage to the checkout-api deploy pipeline so this cannot reach all regions at once.", [(D, "Nice, I'll review the PR.")]),
        ("CDEMOINC", MC, "checkout-api p99 latency creeping up again, watching it.", []),
        ("CDEMOPLAT", D, "checkout-api load test results: handles 3x peak, retries need a cap.", []),
        ("CDEMOPLAT", P, "Postmortem for the checkout-api incident is due Thursday. Dev, can you draft the timeline?", [(D, "On it.")]),
        ("CDEMOINC", MC, "checkout-api error budget is at 40% for the month, freezing risky changes.", []),
        ("CDEMOPLAT", D, "checkout-api retry cap merged. Idempotency keys now required on every payment call.", []),
        ("CDEMOPROD", A, "Will the checkout-api changes affect the Atlas pricing launch date?", [(P, "No, they ship independently.")]),
        ("CDEMOPLAT", MC, "checkout-api dashboards updated with per-region error rates.", []),
        ("CDEMOINC", D, "Alert tuning for checkout-api: paging only when 5xx stays above 2% for five minutes.", []),
        ("CDEMOPLAT", P, "Postmortem review for checkout-api is in the calendar, please read the draft first.", []),
    ],
    "roadmap": [
        ("CDEMOPROD", A, "First pass of the Q4 roadmap is in the doc. Themes: reliability, pricing, onboarding.", [(P, "Reliability needs more room than that."), (A, "Agreed, I'll rebalance.")]),
        ("GDEMOLEAD", P, "Q4 roadmap: engineering capacity is 22 person-weeks after the Phoenix migration.", []),
        ("CDEMOPROD", A, "{me} can you review the Q4 roadmap doc and send me feedback by 5pm today? I need your approval before the board meeting.", []),
        ("GDEMOLEAD", O, "Q4 roadmap budget check: headcount plan fits if hiring slips one month.", []),
        ("CDEMOPROD", A, "Q4 roadmap v2 posted. Moved Orion onboarding up, pushed analytics exports out.", [(K, "Analytics exports slipping hurts the data team.")]),
        ("CDEMOPROD", L, "Design capacity for the Q4 roadmap is tight: Orion onboarding and Atlas pricing both need Lena.", []),
        ("GDEMOLEAD", A, "Q4 roadmap review is Thursday at 11am with the board.", []),
        ("DDEMOANA", A, "Let's schedule a call tomorrow at 3pm to walk through the Q4 roadmap before the board.", []),
        ("CDEMOPROD", P, "Q4 roadmap risks: Phoenix migration slip, checkout-api stability, hiring.", []),
        ("GDEMOLEAD", A, "Q4 roadmap final draft is locked, only typos from here.", []),
        ("CDEMOPROD", A, "Board feedback on the Q4 roadmap: they want clearer metrics per theme.", [(K, "I can pull baselines for each theme.")]),
    ],
    "orion": [
        ("CDEMODSGN", L, "Orion onboarding flow v1 is in Figma: five screens, skippable.", [(A, "Looks great, can we cut the survey step?"), (L, "Yes, moved it to day 3.")]),
        ("CDEMOPROD", A, "Orion onboarding goal: 60% of new accounts connect a data source in the first session.", []),
        ("CDEMODSGN", L, "Orion onboarding empty states need copy. {me} can you draft the tone?", []),
        ("CDEMOPROD", K, "Baseline for Orion onboarding: only 31% connect a data source today.", [(A, "Good, that's the number to beat.")]),
        ("CDEMODSGN", L, "Usability test of Orion onboarding: three of five users missed the skip button.", []),
        ("CDEMOPROD", D, "Orion onboarding needs an API for source health checks. Two days of work.", []),
        ("CDEMODSGN", L, "Orion onboarding v2 makes the skip button a text link at the bottom. Retesting Friday.", []),
        ("CDEMOMKT", S, "Can marketing preview Orion onboarding for the launch post?", [(A, "After the usability retest.")]),
        ("CDEMOPROD", K, "Orion onboarding analytics events defined: step_viewed, source_connected, flow_skipped.", []),
        ("CDEMODSGN", L, "Orion onboarding retest: all five found the skip link. Shipping v2.", []),
    ],
}
NOISE = [
    ("CDEMORAND", "lunch?"), ("CDEMORAND", "anyone up for coffee at 4"), ("CDEMORAND", "FYI the office closes early on Friday"),
    ("CDEMORAND", "happy birthday!! 🎂"), ("CDEMORAND", "this meme is too accurate"), ("CDEMORAND", "thanks!"), ("CDEMORAND", "ok"),
    ("CDEMORAND", "running 5 minutes late"), ("CDEMORAND", "who has the good chargers"), ("CDEMORAND", "standup in 5"),
    ("CDEMORAND", "lol"), ("CDEMORAND", "the printer on 3 is jammed again"), ("CDEMORAND", "anyone want pizza from the new place"),
    ("CDEMOPROD", "FYI the design review slides are in the shared drive."), ("CDEMOMKT", "good morning all"), ("CDEMOPLAT", "+1"),
]
BOT_ALERTS = [
    ("CDEMOINC", "PagerDuty", "[TRIGGERED] checkout-api error rate above 2% (EU)"),
    ("CDEMOINC", "PagerDuty", "[RESOLVED] checkout-api p99 latency back under threshold"),
    ("CDEMOPLAT", "Jenkins", "Build #2481 passed on main (Phoenix schema diff job)"),
    ("CDEMOPLAT", "Jenkins", "Build #2487 failed on main: integration tests (search service)"),
    ("CDEMOPLAT", "GitHub", "PR #912 merged: add canary stage to checkout-api deploy"),
]


# Extra, generated project traffic so the full demo has a realistic volume and priority mix (name -> display token, things, channels, people)
PROJECT_TOPICS = {
    "phoenix": ("Phoenix", ["cutover plan", "replication lag", "rollback steps", "connection pooling", "index rebuild", "DNS change"], ["CDEMOPLAT", "CDEMOINC"], [P, MC, D, K]),
    "atlas": ("Atlas", ["pricing tiers", "pricing page", "discount policy", "billing changes", "launch email", "price test"], ["CDEMOPROD", "CDEMOMKT", "GDEMOLEAD"], [A, S, O, L]),
    "checkout": ("checkout-api", ["error budget", "retry logic", "canary deploy", "latency dashboard", "load test", "alert rules"], ["CDEMOPLAT", "CDEMOINC"], [MC, D, P]),
    "roadmap": ("Q4 roadmap", ["themes", "capacity plan", "metrics", "dependencies", "board deck", "priorities"], ["CDEMOPROD", "GDEMOLEAD"], [A, P, O, K]),
    "orion": ("Orion", ["onboarding flow", "empty states", "analytics events", "usability test", "skip button", "welcome email"], ["CDEMODSGN", "CDEMOPROD"], [L, A, K, S]),
}
EXTRA_UPDATES = ["{p} {t}: made progress today, notes are in the doc.", "Quick update on the {p} {t}, nothing blocking right now.",
                 "Looking at the {p} {t} again, will share findings after lunch.", "Added comments on the {p} {t} in the doc.",
                 "The {p} {t} needs another pass before we call it done.", "Synced with the team about the {p} {t}, we are aligned.",
                 "Finished the draft of the {p} {t}, ready for eyes."]
EXTRA_ASKS = ["{me} can you look at the {p} {t} when you get a minute?", "Please review the {p} {t} by Friday and let me know.",
              "Could you confirm the {p} {t} before the sync tomorrow?", "{me} I need your approval on the {p} {t} by 5pm today.",
              "Can you reply with your thoughts on the {p} {t}? We are waiting on you."]
EXTRA_BLOCKERS = ["We are blocked on the {p} {t}, waiting on a decision.", "Blocked: the {p} {t} depends on a vendor reply, deadline is tomorrow."]


def demo_team(user_id: int) -> str:
    return f"TDEMO{user_id:04d}"


def demo_gmail_team(user_id: int) -> str:
    return f"gmail:demo{user_id}@demo.contextos.test"


EMAIL_PEOPLE = [("Priya Nair", "priya.nair@acme-demo.test"), ("Marcus Chen", "marcus@acme-demo.test"), ("Dev Patel", "dev.patel@vendor-demo.test"),
                ("Ana Ruiz", "ana@acme-demo.test"), ("Finance Team", "finance@acme-demo.test")]
EMAIL_SUBJECTS = ["{p}: {t}", "Re: {p} {t}", "Fwd: {p} {t} (action needed)", "{p} - {t} update"]
EMAIL_BULK = [("Weekly Digest", "newsletter@news-demo.test", "Your weekly digest", "Ten articles you might like. Unsubscribe any time.", True),
              ("Shop Deals", "deals@shop-demo.test", "50% off this weekend only", "Huge sale on everything, don't miss out.", True),
              ("GitHub", "notifications@github.com", "[acme/api] PR #912 merged", "Merged by marcus. View it on GitHub.", True)]


def seed_gmail_demo(db: Session, user: User, size: str = "full") -> int:
    """Emails shaped like Gmail API messages, parsed by the REAL gmail.to_raw and ingested by the real pipeline."""
    import base64
    from app import gmail
    from app.pipeline import ingest_message
    team = demo_gmail_team(user.id)
    me = team.split(":", 1)[1]
    conn = db.query(SlackConnection).filter_by(owner_id=user.id, team_id=team).first()
    if conn is None:
        conn = SlackConnection(owner_id=user.id, team_id=team, slack_user_id=me, team_name=f"{me} (demo Gmail)", provider="gmail", is_demo=True,
                               status="connected", access_level="essential", user_token_encrypted="", granted_user_scopes="gmail.readonly",
                               last_sync_finished_at=utcnow())
        db.add(conn)
        db.commit()
    rnd = random.Random(user.id * 7919)
    now_ms = int(time.time() // 86400 * 86400 * 1000)  # day-aligned so re-seeding the same day is a no-op
    msgs = []

    def add(i, name, addr, subject, body, *, thread, days_ago, to=None, bulk=False):
        headers = [{"name": "From", "value": f"{name} <{addr}>"}, {"name": "To", "value": to or me}, {"name": "Subject", "value": subject}]
        if bulk:
            headers.append({"name": "List-Unsubscribe", "value": "<mailto:u@x.test>"})
        data = base64.urlsafe_b64encode(body.encode()).decode()
        msgs.append({"id": f"demo{user.id}m{i}", "threadId": f"demo{user.id}t{thread}", "internalDate": str(int(now_ms - days_ago * 86400000 - rnd.randint(0, 40000000))),
                     "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": data}}})

    n = 0
    per = 6 if size == "full" else 1
    for key, (pname, things, _chs, _ppl) in PROJECT_TOPICS.items():
        for j in range(per):
            who = EMAIL_PEOPLE[(n + j) % 4]
            t = things[(n + j) % len(things)]
            kind = rnd.choice(("update", "ask", "ask", "block"))
            pool = {"update": EXTRA_UPDATES, "ask": EXTRA_ASKS, "block": EXTRA_BLOCKERS}[kind]
            body = rnd.choice(pool).format(p=pname, t=t, me="Hi")
            add(n, who[0], who[1], rnd.choice(EMAIL_SUBJECTS).format(p=pname, t=t), body, thread=f"{key}{j % 3}", days_ago=rnd.uniform(0, 13))
            n += 1
    add(n, *EMAIL_PEOPLE[4][:2], "Invoice approval needed", "Please approve invoice 4471 by end of day tomorrow. The vendor is waiting on you.", thread="inv", days_ago=0.4); n += 1
    if size == "full":
        for k in range(8):
            nm, ad, sub, body, bulk = EMAIL_BULK[k % len(EMAIL_BULK)]
            add(n, nm, ad, sub, body, thread=f"bulk{k}", days_ago=rnd.uniform(0, 12), bulk=True, to="undisclosed@list.test"); n += 1
    created = []
    for m in msgs:
        raw = gmail.to_raw(m, me)
        if raw is None:
            continue
        res = ingest_message(db, conn, raw["_thread"], raw, conv_type="email", source="demo")
        conv = db.query(SlackConversation).filter_by(connection_id=conn.id, channel_id=raw["_thread"]).first()
        if conv is not None and not conv.name:
            conv.name = raw["_subject"]
            conv.backfill_complete = True
        if res.action == "created":
            created.append(res.message)
    conn.messages_synced = len(created)
    db.commit()
    if created:
        from app.pipeline import index_rows, link_and_log, log_batch
        log_batch(db, conn, created, fetched=len(msgs), trace_id=f"demo-gmail:{user.id}", label="Gmail (demo)")
        db.commit()
        link_and_log(db, created, trace_id=f"demo-gmail:{user.id}")
        index_rows(db, created, f"demo-gmail:{user.id}")
    return len(created)


def _build_events(user_id: int, now: float, size: str = "full") -> list[dict]:
    rng = random.Random(7000 + user_id)
    team = demo_team(user_id)
    seq = [0]
    ev: list[dict] = []

    def ts(seconds_ago: float) -> str:
        seq[0] += 1
        return f"{now - max(5.0, seconds_ago) + seq[0] * 0.000001:.6f}"

    def envelope(event: dict) -> dict:
        n = len(ev) + 1
        return {"type": "event_callback", "team_id": team, "event_id": f"Ev-DEMO-{user_id}-{n:04d}", "event": event,
                "authorizations": [{"team_id": team, "user_id": ME, "is_bot": False}]}

    def add(event: dict) -> dict:
        ev.append(envelope(event))
        return event

    def msg(ch, user, text, seconds_ago, **extra):
        return add({"type": "message", "channel": ch, "channel_type": CHANNELS[ch][2], "user": user,
                    "text": text.replace("{me}", ME_TAG), "ts": ts(seconds_ago), **extra})

    def workday_ago(days: float) -> float:
        """Seconds ago for a time during working hours `days` days back (never in the future)."""
        base = days * 86400 + rng.uniform(0, 8 * 3600)
        return max(300.0, base)

    if size == "small":
        m1 = msg("CDEMOINC", P, "{me} production down: checkout-api is returning 500s for every EU customer. Can you confirm the rollback ASAP?", 95 * 60)
        msg("CDEMOINC", MC, "Rolling back now. I'll post an update in 10 minutes.", 90 * 60, thread_ts=m1["ts"])
        msg("CDEMOINC", P, "Rollback confirmed, error rate is back to normal.", 70 * 60, thread_ts=m1["ts"])
        msg("CDEMOINC", None, "[RESOLVED] checkout-api p99 latency back under threshold", 68 * 60, subtype="bot_message", bot_id="BDEMOPAGER", username="PagerDuty")
        msg("CDEMOPROD", A, "Can you review the Q4 roadmap doc and send me feedback by 5pm today? I need your approval before the board meeting.", 60 * 60)
        msg("CDEMOPROD", MC, "FYI the design review slides are in the shared drive.", 55 * 60)
        msg("DDEMOPRIYA", P, "Let's schedule a call tomorrow at 3pm to sync on the launch plan.", 45 * 60)
        msg("DDEMOPRIYA", P, "Could you also approve the vendor contract by Friday? Legal needs sign-off.", 44 * 60)
        msg("GDEMOLEAD", A, "<!channel> all-hands moved to tomorrow, please confirm you can attend.", 30 * 60)
        msg("GDEMOMPIM", MC, "should we meet later this week to go over pricing? let me know what works.", 25 * 60)
        msg("CDEMORAND", MC, "lunch?", 20 * 60)
        invoice = msg("CDEMOPROD", A, "Please send the invoice to finance by tomorrow.", 15 * 60)
        oops = msg("CDEMOPROD", MC, "Please approve ASAP", 8 * 60)
        msg("CDEMOPROD", ME, "I'll share the launch checklist by tomorrow.", 5 * 60)
        edit_ts = ts(10 * 60)
        add({"type": "message", "subtype": "message_changed", "channel": "CDEMOPROD", "channel_type": "channel", "ts": edit_ts,
             "message": {"type": "message", "user": A, "ts": invoice["ts"], "text": "Please send the invoice to finance by Friday.", "edited": {"user": A, "ts": edit_ts}},
             "previous_message": {"ts": invoice["ts"], "text": invoice["text"]}})
        add({"type": "message", "subtype": "message_deleted", "channel": "CDEMOPROD", "channel_type": "channel", "ts": ts(6 * 60),
             "deleted_ts": oops["ts"], "previous_message": {"ts": oops["ts"], "text": oops["text"]}})
        return ev

    # ---- projects: every beat lands on a day between 13.5 and 0.2 days ago, in order, so projects are discussed across days
    for name, beats in PROJECT_BEATS.items():
        n = len(beats)
        for i, (ch, author, text, replies) in enumerate(beats):
            day = 13.5 - (13.3 * i / max(1, n - 1)) + rng.uniform(-0.4, 0.4)
            t = workday_ago(min(13.9, max(0.2, day)))
            parent = msg(ch, author, text, t)
            for j, (ra, rtext) in enumerate(replies):
                msg(ch, ra, rtext, t - (j + 1) * rng.uniform(600, 5400), thread_ts=parent["ts"])
    # ---- generated per-project traffic (updates, asks, blockers) so each project spans many days, people and channels
    for key, (tok, things, chans, people) in PROJECT_TOPICS.items():
        for i in range(26):
            kind = rng.random()
            tpl = rng.choice(EXTRA_ASKS if kind < 0.28 else EXTRA_BLOCKERS if kind < 0.36 else EXTRA_UPDATES)
            text = tpl.format(p=tok, t=rng.choice(things), me="{me}")
            msg(rng.choice(chans), rng.choice(people), text, workday_ago(rng.uniform(0.1, 13.8)))
    # ---- your own replies (so the "waiting on you" list is meaningful: some mentions stay unanswered)
    for ch, text, d in [("CDEMOPLAT", "Marcus, I'll own rollback for Phoenix.", 12.0), ("CDEMOPROD", "Atlas tiers look right to me, one comment on naming.", 6.1),
                        ("CDEMOINC", "Rollback is confirmed, thanks everyone.", 4.9), ("CDEMOPROD", "Approved the Q4 roadmap themes.", 5.5),
                        ("CDEMODSGN", "Happy to draft the Orion empty-state tone tomorrow.", 3.2), ("CDEMOPLAT", "Retry cap looks good, approving.", 2.4),
                        ("GDEMOLEAD", "Fine with the grandfathering plan for Atlas.", 2.0)]:
        msg(ch, ME, text, workday_ago(d))
    # ---- chatter and bot alerts across the two weeks
    for _ in range(70):
        ch, text = rng.choice(NOISE)
        msg(ch, rng.choice(list(set(PEOPLE) - {ME})), text, workday_ago(rng.uniform(0.1, 13.8)))
    for _ in range(14):
        ch, bot, text = rng.choice(BOT_ALERTS)
        msg(ch, None, text, workday_ago(rng.uniform(0.1, 13.5)), subtype="bot_message", bot_id="B" + bot.upper()[:8], username=bot)
    # ---- recent, deadline-bearing requests (reminders and meeting suggestions come from these)
    msg("CDEMOPROD", A, "Please send the Atlas invoice to finance by tomorrow.", 15 * 60)
    invoice = msg("CDEMOPROD", O, "Please send the vendor invoice to finance by tomorrow.", 16 * 60)
    msg("DDEMOPRIYA", P, "Let's schedule a call tomorrow at 3pm to sync on the Phoenix launch plan.", 45 * 60)
    msg("DDEMOPRIYA", P, "Could you also approve the vendor contract by Friday? Legal needs sign-off.", 44 * 60)
    msg("GDEMOLEAD", A, "<!channel> all-hands moved to tomorrow, please confirm you can attend.", 30 * 60)
    msg("GDEMOMPIM", MC, "should we meet later this week to go over Atlas pricing? let me know what works.", 25 * 60)
    msg("DDEMOANA", A, "{me} quick one: can you reply with the Orion onboarding goal numbers before the 4pm sync?", 20 * 60)
    oops = msg("CDEMOPROD", MC, "Please approve ASAP", 8 * 60)
    # ---- edits, deletes and replays: the pipeline's integrity story
    edit_ts = ts(10 * 60)
    add({"type": "message", "subtype": "message_changed", "channel": "CDEMOPROD", "channel_type": "channel", "ts": edit_ts,
         "message": {"type": "message", "user": O, "ts": invoice["ts"], "text": "Please send the vendor invoice to finance by Friday.", "edited": {"user": O, "ts": edit_ts}},
         "previous_message": {"ts": invoice["ts"], "text": invoice["text"]}})
    add({"type": "message", "subtype": "message_deleted", "channel": "CDEMOPROD", "channel_type": "channel", "ts": ts(6 * 60),
         "deleted_ts": oops["ts"], "previous_message": {"ts": oops["ts"], "text": oops["text"]}})
    return ev


def seed_demo(db: Session, user: User, *, size: str = "full", process: bool = False) -> dict:
    team = demo_team(user.id)
    conn = db.query(SlackConnection).filter_by(owner_id=user.id, team_id=team).first()
    if conn is None:
        conn = SlackConnection(owner_id=user.id, team_id=team, slack_user_id=ME, team_name="Acme (demo workspace)", is_demo=True,
                               status="connected", access_level="with_replies", user_token_encrypted="",
                               granted_user_scopes=",".join(sorted(ACCESS_LEVELS["with_replies"]["scopes"])), last_sync_finished_at=utcnow())
        db.add(conn)
        db.flush()
    for uid, name in PEOPLE.items():
        if not db.query(SlackUser).filter_by(team_id=team, user_id=uid).first():
            db.add(SlackUser(team_id=team, user_id=uid, display_name=name))
    for cid, (name, ctype, _) in CHANNELS.items():
        if not db.query(SlackConversation).filter_by(connection_id=conn.id, channel_id=cid).first():
            db.add(SlackConversation(connection_id=conn.id, channel_id=cid, name=name, conv_type=ctype,
                                     peer_user_id={"DDEMOPRIYA": "UDEMOPRIYA", "DDEMOANA": "UDEMOANA"}.get(cid), backfill_complete=True))
    db.commit()

    accepted = duplicates = 0
    events = _build_events(user.id, time.time(), size)
    for ev in events:
        res = accept_event(db, ev)
        accepted += bool(res.get("queued"))
        duplicates += bool(res.get("duplicate"))
    for i in range(0, min(len(events), 5 if size == "full" else 1)):   # Slack redelivers events: the receipt table must drop replays
        duplicates += bool(accept_event(db, events[i * 7 % len(events)]).get("duplicate"))
    emails = seed_gmail_demo(db, user, size)
    processed = 0
    if process:
        from app.worker import run_pending_jobs
        processed = run_pending_jobs(50000, wait_seconds=120)
    invalidate_user_caches(user.id)
    return {"connection_id": conn.id, "size": size, "events_accepted": accepted, "duplicate_events_dropped": duplicates,
            "jobs_processed_inline": processed, "emails_ingested": emails,
            "note": "Events went through accept_event -> slack_event job -> fan-out -> slack_process_message -> pipeline -> projects -> summaries."}


def remove_demo(db: Session, user: User) -> dict:
    """Deletes the demo workspace and everything derived from it. Projects built from demo messages are removed too,
    because discovery re-runs on whatever is left (and a user with no messages left has no projects)."""
    from app.models import MessageProject, Project, ProjectKeyword, ProjectSummary
    team = demo_team(user.id)
    removed = 0
    for conn in db.query(SlackConnection).filter_by(owner_id=user.id, is_demo=True).all():
        removed += purge_connection_data(db, conn)
        db.query(OutboundAction).filter(OutboundAction.connection_id == conn.id).delete(synchronize_session=False)
        db.query(ProcessingJob).filter(ProcessingJob.connection_id == conn.id).delete(synchronize_session=False)
        db.delete(conn)
    db.query(ProcessingJob).filter(ProcessingJob.team_id == team).delete(synchronize_session=False)
    db.query(EventReceipt).filter(EventReceipt.event_id.like(f"Ev-DEMO-{user.id}-%")).delete(synchronize_session=False)
    db.query(SlackUser).filter(SlackUser.team_id == team).delete(synchronize_session=False)
    has_real = db.query(SlackConnection.id).filter(SlackConnection.owner_id == user.id, SlackConnection.is_demo.is_(False)).first() is not None
    if not has_real:
        pids = [i for (i,) in db.query(Project.id).filter_by(owner_id=user.id).all()]
        db.query(MessageProject).filter(MessageProject.owner_id == user.id).delete(synchronize_session=False)
        db.query(ProjectSummary).filter(ProjectSummary.owner_id == user.id).delete(synchronize_session=False)
        if pids:
            db.query(ProjectKeyword).filter(ProjectKeyword.project_id.in_(pids)).delete(synchronize_session=False)
        db.query(Project).filter(Project.owner_id == user.id).delete(synchronize_session=False)
        db.query(PipelineEvent).filter(PipelineEvent.owner_id == user.id, PipelineEvent.stage != "login").delete(synchronize_session=False)
        u = db.get(User, user.id)
        if u is not None:
            u.projects_discovered_at = None
    db.commit()
    invalidate_user_caches(user.id)
    return {"removed_messages": removed}


def main() -> None:
    from app.database import SessionLocal, init_db
    parser = argparse.ArgumentParser(description="Seed demo Slack data through the real pipeline")
    parser.add_argument("--email", required=True, help="email of an existing ContextOS user")
    parser.add_argument("--size", choices=("full", "small"), default="full")
    parser.add_argument("--reset", action="store_true", help="remove demo data instead")
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        user = db.query(User).filter_by(email=args.email.strip().lower()).first()
        if not user:
            raise SystemExit(f"No user {args.email}. Sign in once in the UI first.")
        print(remove_demo(db, user) if args.reset else seed_demo(db, user, size=args.size, process=True))


if __name__ == "__main__":
    main()
