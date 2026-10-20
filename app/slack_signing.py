import hashlib, hmac, time
from app.config import settings

class InvalidSlackSignature(Exception):
    pass

def verify_slack_request(raw_body: bytes, timestamp: str | None, signature: str | None):
    if not timestamp or not signature:
        raise InvalidSlackSignature("Missing Slack signature headers")
    try:
        request_time = int(timestamp)
    except ValueError as exc:
        raise InvalidSlackSignature("Invalid Slack timestamp") from exc
    if abs(time.time() - request_time) > 300:
        raise InvalidSlackSignature("Slack request timestamp outside tolerance")
    if not settings.slack_signing_secret:
        raise InvalidSlackSignature("Configure SLACK_SIGNING_SECRET in .env first")
    base = f"v0:{timestamp}:".encode() + raw_body
    digest = hmac.new(settings.slack_signing_secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(f"v0={digest}", signature):
        raise InvalidSlackSignature("Invalid Slack signature")
