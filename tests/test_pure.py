from datetime import datetime, timezone

import pytest

from app.crypto import TokenCryptoError, decrypt_token, encrypt_token
from app.priority import extract_due, extract_meeting_suggestion, get_tz, score_message
from app.slack_text import has_broadcast, normalize_text, ts_max, ts_min, mentions_user

NOW = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)  # Sunday 10:30 IST


def test_normalize_mrkdwn():
    out = normalize_text("<@U1> see <https://x.io|the doc> &amp; <#C1|eng> <!here> don\u2019t", {"U1": "Priya"})
    assert out == "@Priya see the doc & #eng @here don't"


def test_mentions_and_broadcast():
    assert mentions_user("hi <@UABC>", "UABC") and not mentions_user("hi <@UXYZ>", "UABC")
    assert has_broadcast("<!channel> hi") and not has_broadcast("hi")


def test_ts_ordering_is_decimal_safe():
    assert ts_max("1700000000.000002", "1700000000.000010") == "1700000000.000010"
    assert ts_min("1700000000.000002", "1700000000.000010") == "1700000000.000002"


def test_p0_and_negation():
    assert score_message("production down, please confirm rollback ASAP")[1] == "P0"
    assert score_message("this is not urgent")[1] == "P3"


def test_context_boosts():
    base = score_message("can you look at this")[0]
    assert score_message("can you look at this", mentions_me=True, is_dm=True)[0] > base


def test_due_dates_use_app_timezone():
    due, explicit = extract_due("send by 5pm today", NOW, get_tz())
    assert explicit and due.astimezone(get_tz()).strftime("%H:%M") == "17:00"
    due, _ = extract_due("approve by Friday", NOW, get_tz())
    assert due.astimezone(get_tz()).strftime("%a %H:%M") == "Fri 17:00"


def test_meeting_provisional_flag():
    _, start, provisional = extract_meeting_suggestion("meeting later this week?", NOW)
    assert provisional
    _, start, provisional = extract_meeting_suggestion("let's call tomorrow at 3pm", NOW)
    assert not provisional and start.astimezone(get_tz()).strftime("%H:%M") == "15:00"


def test_token_roundtrip_and_ciphertext():
    blob = encrypt_token("xoxp-secret")
    assert "xoxp" not in blob and decrypt_token(blob) == "xoxp-secret"
    with pytest.raises(TokenCryptoError):
        decrypt_token("not-a-token")


def test_ranker_is_rules_only_v741():
    from datetime import datetime, timedelta, timezone
    from app import ranker
    now = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    # urgency curve: no deadline -> 0, near deadline > far deadline, bounded, and continuous across the seam (x = 0)
    assert ranker.urgency(None) == 0.0 and ranker.urgency(1) > ranker.urgency(48)
    seam = ranker.EFFORT_H + ranker.BUFFER_H
    assert abs(ranker.urgency(seam - 1e-6) - ranker.urgency(seam + 1e-6)) < 1e-4
    assert 0.0 <= ranker.urgency(-100) <= 1.0
    # a direct request with a deadline outranks general chatter, and each score comes with one plain sentence
    hot = ranker.rank_message("Can you approve the invoice by 5pm today?", now, "Priya Nair", is_dm=True, mentions_me=False, is_bot=False, now=now)
    cold = ranker.rank_message("anyone up for lunch", now, "Sam", is_dm=False, mentions_me=False, is_bot=False, now=now)
    assert hot[0] > cold[0] and hot[1] in ("P1", "P0") and cold[1] == "P3"
    assert hot[2].startswith("Priya needs your approval") and hot[2].count(".") == 1 and "wrote to you directly" in hot[2]
    # older messages fade, and the user's labels move the sender weight
    old = ranker.rank_message("Can you approve the invoice?", now - timedelta(days=4), "Priya", is_dm=True, mentions_me=False, is_bot=False, now=now)
    fresh = ranker.rank_message("Can you approve the invoice?", now, "Priya", is_dm=True, mentions_me=False, is_bot=False, now=now)
    assert fresh[0] > old[0]
    liked = ranker.rank_message("Can you approve the invoice?", now, "Priya", is_dm=True, mentions_me=False, is_bot=False, sender_shift=-0.4, now=now)
    assert liked[0] < fresh[0]


def test_deadline_explanation_formats_time_portably():
    from app import ranker

    due = datetime(2026, 11, 2, 5, 0, tzinfo=get_tz())
    assert ranker._when(due, 0).startswith("5 AM ")


def test_assistant_prose_safety_net_turns_lists_into_sentences():
    from app.assistant import _as_prose, _say
    out = _as_prose("## Status\n**Phoenix** is blocked.\n- Priya has two blockers [1]\n- Dev waits on a vendor [2]\n\nThat's it.")
    assert "- " not in out and "#" not in out and "**" not in out
    assert "Priya has two blockers [1]. Dev waits on a vendor [2]." in out
    assert _say(["a", "b", "c"]) == "a, b, and c" and _say(["a", "b"]) == "a and b"
