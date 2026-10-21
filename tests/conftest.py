import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SLACK_CLIENT_ID", "cid")
os.environ.setdefault("SLACK_CLIENT_SECRET", "secret")
os.environ.setdefault("SLACK_SIGNING_SECRET", "signing")
os.environ.setdefault("GOOGLE_CLIENT_ID", "gid")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "gsecret")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("LLM_PROVIDER", "none")
os.environ.setdefault("APP_TIMEZONE", "Asia/Kolkata")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.orm import sessionmaker
    import app.database as database
    import app.models  # noqa: F401
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    database.SessionLocal.configure(bind=engine)  # worker.handle_one() uses SessionLocal
    with Session() as s:
        yield s


@pytest.fixture()
def user(db):
    from app.models import User
    u = User(email="a@example.com", name="A", password_hash="x")
    db.add(u); db.commit()
    return u


@pytest.fixture()
def conn(db, user):
    from app.crypto import encrypt_token
    from app.models import SlackConnection
    c = SlackConnection(owner_id=user.id, team_id="T1", slack_user_id="UME", team_name="Acme", user_token_encrypted=encrypt_token("xoxp-test"),
                        granted_user_scopes="channels:history,channels:read,groups:history,groups:read,im:history,im:read,mpim:history,mpim:read,chat:write,users:read")
    db.add(c); db.commit()
    return c
