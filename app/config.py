from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://contextos:contextos_dev_password@localhost:5432/contextos"
    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    contextos_admin_key: str = ""
    app_env: str = "development"
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
    app_timezone: str = "Asia/Kolkata"
    worker_poll_seconds: float = 1.0
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
