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
    assert score_message("can you look at this", mentions_me=True, is_dm=True)[0] == base + 25
    score, label, reason = score_message("meet at 8")
    assert score == 40 and label == "P2" and "meeting" in reason
    score, label, _ = score_message("meet today")
    assert score == 65 and label == "P1"


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
    _, start, provisional = extract_meeting_suggestion("meet at 8", NOW)
    assert provisional and start.astimezone(get_tz()).strftime("%Y-%m-%d %H:%M") == "2026-10-18 20:00"


def test_calendar_invite_extracts_explicit_date_and_time_range():
    text = "78catalyst - Cohort Session5 Wednesday, October 21 · 8:00 – 9:00pm Time zone: Asia/Kolkata Google Meet joining info"
    _, start, provisional = extract_meeting_suggestion(text, datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))
    assert not provisional
    assert start.astimezone(get_tz()).strftime("%Y-%m-%d %H:%M") == "2026-10-21 20:00"
    assert score_message(text, is_bot=True)[1] == "P1"


def test_token_roundtrip_and_ciphertext():
    blob = encrypt_token("xoxp-secret")
    assert "xoxp" not in blob and decrypt_token(blob) == "xoxp-secret"
    with pytest.raises(TokenCryptoError):
        decrypt_token("not-a-token")
