"""Runtime settings, read from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLite keeps local development zero-setup; production uses Postgres, e.g.
    # postgresql+psycopg://user:pass@host:5432/voiceagent
    database_url: str = "sqlite:///./voiceagent.db"

    # Claude
    anthropic_api_key: str | None = None
    # Haiku for live call turns: callers notice any pause, so latency comes first.
    live_model: str = "claude-haiku-5-5"
    # Sonnet for offline work such as tagging feedback transcripts.
    analysis_model: str = "claude-sonnet-5-5"
    live_effort: str = "low"

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    # Public base URL of this service, e.g. https://voice.example.com (used to build
    # the ConversationRelay WebSocket URL handed to Twilio).
    public_base_url: str = "http://localhost:8000"

    # Feedback calls
    feedback_delay_minutes: int = 30
    feedback_max_attempts: int = 3
    calling_hours_start: int = 10  # local hour, inclusive
    calling_hours_end: int = 21  # local hour, exclusive


@lru_cache
def get_settings() -> Settings:
    return Settings()
