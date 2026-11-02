import pytest

from app.config import Settings


def _s(**env):
    import os
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return Settings(_env_file=None)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_supabase_style_url_gets_driver_and_ssl():
    s = _s(DATABASE_URL="postgresql://postgres:pw@db.abc.supabase.co:5432/postgres")
    assert s.database_url == "postgresql+psycopg2://postgres:pw@db.abc.supabase.co:5432/postgres?sslmode=require"


def test_local_url_is_left_without_ssl():
    s = _s(DATABASE_URL="postgres://u:p@localhost:5432/d")
    assert s.database_url == "postgresql+psycopg2://u:p@localhost:5432/d"


def test_empty_database_password_is_a_fatal_startup_problem():
    s = _s(DATABASE_URL="postgresql://postgres:@db.abc.supabase.co:5432/postgres")
    assert any(p.startswith("FATAL") and "no password" in p for p in s.production_problems())


def test_singular_admin_email_and_url_whitespace_are_accepted():
    s = _s(ADMIN_EMAIL="Boss@Example.com", PUBLIC_BASE_URL=" https://x.ngrok-free.dev/ ")
    assert s.admin_email_set == {"boss@example.com"} and s.public_base_url == "https://x.ngrok-free.dev"


def test_unreachable_database_gives_one_readable_error(monkeypatch):
    from sqlalchemy import create_engine
    import app.database as d
    monkeypatch.setattr(d, "engine", create_engine("sqlite:////nonexistent_dir/x.db"))
    with pytest.raises(RuntimeError, match="Cannot connect to the database"):
        d.wait_for_database(attempts=1, delay=0)
