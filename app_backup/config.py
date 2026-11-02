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


# What each permission level asks Slack for. The UI explains these in plain words; the scopes are the technical truth.
# Levels are cumulative so a user can start light and upgrade later without losing anything.
ACCESS_LEVELS: dict[str, dict] = {
    "essential": {
        "label": "Public channels only",
        "scopes": ["channels:history", "channels:read", "users:read"],
    },
    "recommended": {
        "label": "All my conversations (read-only)",
        "scopes": ["channels:history", "channels:read", "groups:history", "groups:read", "im:history", "im:read",
                   "mpim:history", "mpim:read", "users:read"],
    },
    "with_replies": {
        "label": "All my conversations + send replies I approve",
        "scopes": ["channels:history", "channels:read", "groups:history", "groups:read", "im:history", "im:read",
                   "mpim:history", "mpim:read", "users:read", "chat:write"],
    },
}
DEFAULT_ACCESS_LEVEL = "recommended"


class Settings(BaseSettings):
    # --- core -------------------------------------------------------------
    database_url: str = "postgresql+psycopg2://contextos:contextos_dev_password@localhost:5432/contextos"
    app_env: str = "development"
    app_timezone: str = "Asia/Kolkata"
    worker_poll_seconds: float = 1.0
    auto_create_tables: bool = True          # dev convenience; use Alembic in real deployments
    public_base_url: str = "http://localhost:8000"   # set to your ngrok https URL
    max_body_bytes: int = 1_000_000          # hard cap on request bodies (Slack events are a few KB)

    # --- sessions / auth --------------------------------------------------
    session_ttl_hours: int = 24 * 7
    session_cookie_name: str = "contextos_session"
    max_sessions_per_user: int = 10
    allow_registration: bool = True
    allow_password_auth: bool = True         # false = Google sign-in only

    # --- Google sign-in ---------------------------------------------------
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""            # default: {PUBLIC_BASE_URL}/auth/google/callback
    google_calendar_redirect_uri: str = ""    # default: {PUBLIC_BASE_URL}/integrations/google-calendar/callback

    # --- rate limits (Redis-backed when REDIS_URL works, otherwise per-process) --------------------
    trusted_proxy_hops: int = 0              # 1 behind ngrok / one load balancer. 0 = trust only the TCP peer.
    rl_login_ip_per_5min: int = 30
    rl_login_email_fail_per_hour: int = 20
    rl_login_fail_lock_after: int = 5        # failures per (email, ip) in 15 min before an exponential lock-out starts
    rl_register_ip_per_hour: int = 10
    rl_oauth_ip_per_5min: int = 40
    rl_api_user_per_min: int = 300           # every authenticated call
    rl_expensive_user_per_hour: int = 30     # discover / summarise / reindex
    rl_assistant_user_per_hour: int = 60

    # --- Slack ------------------------------------------------------------
    slack_signing_secret: str = ""
    slack_bot_token: str = ""                # legacy global token: no longer used for sending/syncing
    contextos_admin_key: str = ""            # legacy admin-key flow: deprecated
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_redirect_uri: str = ""             # default: {PUBLIC_BASE_URL}/integrations/slack/callback
    slack_user_scopes: str = ""              # DEPRECATED: scopes now come from the permission level the user picks
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

    # --- retention (see ARCHITECTURE.md "Data lifecycle") --------------------
    retention_enabled: bool = True
    retention_p3_compact_days: int = 7       # P3 chatter: keep metadata + a short snippet only
    retention_p3_delete_days: int = 30       # ...and drop it entirely after this
    retention_keep_days: int = 180           # P0-P2 are kept this long
    max_messages_per_user: int = 50000       # hard cap; oldest P3 then P2 are pruned first
    snippet_chars: int = 200

    # --- projects ---------------------------------------------------------
    project_window_days: int = 30
    project_min_messages: int = 5
    project_min_days: int = 2
    project_max: int = 12
    project_discover_minutes: int = 15       # how often the scheduler re-runs discovery for a user with new messages

    semantic_link_min_score: float = 0.50    # Qdrant: min cosine similarity to link a keyword-less message to a project summary
    semantic_link_max_per_run: int = 300
    collaborator_min_projects: int = 2       # a person active in this many projects is a 'key collaborator'
    collaborator_min_messages: int = 4
    collaborator_boost: int = 10             # priority points added to their recent P2/P3 messages

    # --- LLM (any OpenAI-compatible endpoint; free-tier friendly) ------------
    # Presets (set in .env):
    #   Groq    LLM_BASE_URL=https://api.groq.com/openai/v1                           LLM_MODEL=llama-3.3-70b-versatile
    #   Gemini  LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai  LLM_MODEL=<a Flash / Flash-Lite model>
    # Free-tier limits change often: set LLM_RPM / LLM_DAILY_CAP a little UNDER what your provider's console shows.
    llm_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_rpm: int = 25
    llm_daily_cap: int = 900
    llm_user_daily_cap: int = 150
    llm_timeout_seconds: float = 40.0
    llm_max_retries: int = 2
    summary_max_messages: int = 40
    summary_message_chars: int = 300
    summary_min_new_messages: int = 3
    summary_interval_minutes: int = 30

    # --- who may open the Pipeline page / operational endpoints ---------------
    # Comma-separated e-mail addresses. In production an admin must also have a Google-verified address.
    # Empty: in development the FIRST account created is the admin; in production nobody is (and startup warns).
    admin_emails: str = ""

    # --- Qdrant / Neo4j driven features (all tunable) -------------------------
    semantic_search_min_score: float = 0.30  # Smart search: minimum cosine similarity for a "by meaning" hit
    related_min_score: float = 0.45          # "Related messages": minimum cosine similarity
    merge_suggest_min_score: float = 0.80    # Projects whose SUMMARIES are this similar are offered for merging
    escalation_min_score: float = 0.78       # A quiet message this close to an urgent one is raised a little
    escalation_boost: int = 8
    escalation_window_days: int = 3
    dependency_share: float = 0.60           # One person wrote >= this share of a project's messages: single point of failure
    dependency_min_messages: int = 8
    index_backfill_batch: int = 64

    # --- pipeline page ----------------------------------------------------
    pipeline_event_retention_days: int = 7
    pipeline_event_cap_per_user: int = 5000

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
    def google_redirect(self) -> str:
        return (self.google_redirect_uri or f"{self.public_base_url.rstrip('/')}/auth/google/callback").strip()

    @property
    def google_calendar_redirect(self) -> str:
        return (self.google_calendar_redirect_uri or
                f"{self.public_base_url.rstrip('/')}/integrations/google-calendar/callback").strip()

    @property
    def bot_scope_list(self) -> list[str]:
        return [s.strip() for s in self.slack_bot_scopes.split(",") if s.strip()]

    @property
    def cookie_secure(self) -> bool:
        return self.public_base_url.lower().startswith("https://")

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in ("production", "prod", "staging")

    @property
    def demo_enabled(self) -> bool:
        return self.enable_demo if self.enable_demo is not None else self.app_env == "development"

    @property
    def slack_oauth_configured(self) -> bool:
        return bool(self.slack_client_id and self.slack_client_secret)

    @property
    def google_oauth_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key.strip() and self.llm_model.strip() and _has_host(self.llm_base_url))

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def qdrant_enabled(self) -> bool:
        return _has_host(self.qdrant_url)

    @property
    def neo4j_enabled(self) -> bool:
        return _has_host(self.neo4j_uri)

    def scopes_for_level(self, level: str) -> list[str]:
        return list(ACCESS_LEVELS.get(level, ACCESS_LEVELS[DEFAULT_ACCESS_LEVEL])["scopes"])

    # --- startup self-check -------------------------------------------------
    def production_problems(self) -> list[str]:
        """Human-readable configuration problems. Logged at startup; in production the first group is fatal."""
        fatal, warn = [], []
        if not self.token_encryption_key.strip():
            fatal.append("TOKEN_ENCRYPTION_KEY is empty: Slack tokens cannot be stored. Generate one with `python -m app.crypto`.")
        if self.is_production:
            if not self.cookie_secure:
                fatal.append("PUBLIC_BASE_URL must be https:// in production (cookies would be sent without the Secure flag).")
            if self.auto_create_tables:
                warn.append("AUTO_CREATE_TABLES=true in production: use `alembic upgrade head` and set it to false.")
            if self.demo_enabled:
                fatal.append("Demo endpoints are enabled in production (set ENABLE_DEMO=false).")
            if not self.slack_signing_secret:
                fatal.append("SLACK_SIGNING_SECRET is empty: live Slack events would be rejected.")
        if not self.redis_url:
            warn.append("REDIS_URL is empty: rate limits are per-process and reset on restart (fine for one dev process, not for production).")
        if self.cookie_secure and self.trusted_proxy_hops == 0 and "localhost" not in self.public_base_url:
            warn.append("PUBLIC_BASE_URL is public https but TRUSTED_PROXY_HOPS=0: behind ngrok/a load balancer every visitor shares one IP, "
                        "so per-IP limits become global. Set TRUSTED_PROXY_HOPS=1.")
        if not self.google_oauth_configured:
            warn.append("GOOGLE_CLIENT_ID/SECRET are empty: 'Continue with Google' is hidden; only email+password sign-in works.")
        if not self.allow_password_auth and not self.google_oauth_configured:
            fatal.append("Password sign-in is disabled and Google sign-in is not configured: nobody can log in.")
        if not self.qdrant_enabled:
            warn.append("QDRANT_URL is empty: no search by meaning, related messages, merge suggestions or semantic project links. "
                        "Run `docker compose up -d qdrant` and set QDRANT_URL=http://localhost:6333.")
        if not self.neo4j_enabled:
            warn.append("NEO4J_URI is empty: People page, 'who to ask', collaborator boost and dependency warnings fall back to slower PostgreSQL approximations. "
                        "Run `docker compose up -d neo4j` and set NEO4J_URI=bolt://localhost:7687 (+ NEO4J_USER / NEO4J_PASSWORD).")
        if self.is_production and not self.admin_email_set:
            warn.append("ADMIN_EMAILS is empty: nobody can open the Pipeline page in production.")
        if not self.llm_enabled:
            warn.append("LLM_API_KEY is empty: summaries and answers use the extractive (non-LLM) fallback.")
        return [f"FATAL: {m}" for m in fatal] + [f"warning: {m}" for m in warn]


settings = Settings()
