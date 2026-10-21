from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


def _has_host(url: str) -> bool:
    """True only for URLs with a real host. Placeholders such as 'https://' or 'neo4j+s://' count as unset."""
    if not url:
        return False
    try:
        return bool(urlparse(url.strip()).netloc)
    except ValueError:
        return False


class Settings(BaseSettings):
    # --- core -------------------------------------------------------------
    database_url: str = "postgresql+psycopg2://contextos:BMSCE2026s@localhost:5432/contextos"
    app_env: str = "development"
    app_timezone: str = "Asia/Kolkata"
    worker_poll_seconds: float = 1.0
    auto_create_tables: bool = True          # dev convenience; use Alembic in real deployments
    public_base_url: str = "http://localhost:8000"   # set to your ngrok https URL

    # --- sessions / auth --------------------------------------------------
    session_ttl_hours: int = 24 * 7
    session_cookie_name: str = "contextos_session"
    allow_registration: bool = True
    admin_email: str = ""

    # --- Slack ------------------------------------------------------------
    slack_signing_secret: str = ""
    slack_bot_token: str = ""                # legacy global token: no longer used for sending/syncing
    contextos_admin_key: str = ""            # legacy admin-key flow: deprecated
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_redirect_uri: str = ""             # default: {PUBLIC_BASE_URL}/integrations/slack/callback
    slack_bot_scopes: str = ""               # optional; user-token flow needs none
    token_encryption_key: str = ""           # Fernet key(s), comma separated for rotation

    # --- sync behaviour ---------------------------------------------------
    slack_initial_sync_days: int = 14
    slack_history_page_size: int = 200       # set to 15 if Slack throttles your app (non-Marketplace distributed apps)
    slack_max_messages_per_conversation: int = 5000
    slack_max_inline_wait_seconds: int = 20  # longer 429 waits are rescheduled instead of blocking the worker
    slack_auto_sync_minutes: int = 5
    slack_reconcile_hours: int = 6
    slack_reconcile_days: int = 3
    ingest_bot_messages: bool = True
    job_lock_timeout_seconds: int = 600
    enable_demo: bool | None = None          # default: enabled when APP_ENV=development

    # --- login hardening / Google sign-in ---------------------------------
    google_client_id: str = ""               # Google Cloud > Credentials > OAuth client (Web). Separate from Calendar.
    google_client_secret: str = ""
    login_max_attempts: int = 5              # failed logins per email before a lockout
    login_window_seconds: int = 900
    login_lockout_seconds: int = 900         # doubles on each repeated lockout, capped at 24 h
    login_ip_max_attempts: int = 25
    trust_proxy: bool = True                 # read X-Forwarded-For (ngrok/reverse proxy). Set false if exposed directly.
    max_body_bytes: int = 1_000_000

    # --- LLM (free tiers; all optional, the app falls back to extractive answers) ---
    llm_provider: str = "auto"               # auto | groq | gemini | none
    groq_api_key: str = ""
    groq_model_fast: str = "llama-3.1-8b-instant"       # bulk summaries/drafts: highest free requests/day
    groq_model_quality: str = "llama-3.3-70b-versatile" # Ask answers
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"
    llm_rpm: int = 25                        # stay under the provider's free per-minute limit
    llm_rpd: int = 1000                      # and under its per-day limit (check your provider console)
    llm_timeout_seconds: int = 25

    # --- projects / data lifecycle ----------------------------------------
    project_min_messages: int = 3
    project_lookback_days: int = 90
    retain_p3_days: int = 30                 # low-value chatter expires early
    retain_all_days: int = 180
    pipeline_event_retention_days: int = 3

    # --- optional integrations -------------------------------------------
    redis_url: str = ""
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: str = ""
    google_calendar_enabled: bool = False
    google_calendar_credentials_file: str = "credentials.json"
    google_calendar_token_file: str = "token.json"
    google_calendar_id: str = "primary"
    qdrant_collection: str = "contextos_messages"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    neo4j_database: str = "neo4j"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- derived ----------------------------------------------------------
    @property
    def redirect_uri(self) -> str:
        return (self.slack_redirect_uri or f"{self.public_base_url.rstrip('/')}/integrations/slack/callback").strip()

    @property
    def bot_scope_list(self) -> list[str]:
        return [s.strip() for s in self.slack_bot_scopes.split(",") if s.strip()]

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/google/callback"

    @property
    def cookie_secure(self) -> bool:
        return self.public_base_url.lower().startswith("https://")

    @property
    def demo_enabled(self) -> bool:
        return self.enable_demo if self.enable_demo is not None else self.app_env == "development"

    @property
    def slack_oauth_configured(self) -> bool:
        return bool(self.slack_client_id and self.slack_client_secret)

    @property
    def qdrant_enabled(self) -> bool:
        return _has_host(self.qdrant_url)

    @property
    def neo4j_enabled(self) -> bool:
        return _has_host(self.neo4j_uri)


settings = Settings()
