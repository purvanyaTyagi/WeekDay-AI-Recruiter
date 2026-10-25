from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    app_name: str = "AI Recruiter"
    base_url: str = "http://localhost:8000"  # public URL used in interview links
    secret_key: str = "change-me-in-production"
    database_url: str = f"sqlite:///{BASE_DIR / 'recruiter.db'}"
    upload_dir: Path = BASE_DIR / "uploads"
    max_upload_mb: int = 10
    app_timezone: str = "UTC"
    session_https_only: bool = False

    # Auth (single recruiter)
    recruiter_username: str = "recruiter"
    recruiter_password_hash: str = ""  # bcrypt hash; generate with scripts/hash_password.py

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    outreach_model: str = ""
    interview_model: str = ""
    briefing_model: str = ""  # falls back to outreach_model
    summary_model: str = ""  # falls back to interview_model
    llm_timeout_seconds: float = 60.0

    # Outreach safety rails
    default_send_mode: str = "auto"  # auto | review
    max_outbound_per_candidate_per_day: int = 3
    quiet_hours_start: str = "21:00"  # local time (candidate timezone)
    quiet_hours_end: str = "08:00"
    recruiter_name: str = "the hiring team"
    recruiter_notify_email: str = ""  # optional: email the recruiter about no-shows, opt-outs, etc.
    company_name: str = "Our Company"

    # Email (SMTP send + IMAP receive). Console adapter is used when smtp_host is empty.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True  # STARTTLS on smtp_port; set smtp_port=465 for implicit SSL
    email_from: str = ""
    email_from_name: str = "AI Recruiter"
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_poll_seconds: int = 60

    # SMS (Twilio). Console adapter is used when credentials are empty.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_validate_signature: bool = True

    # Interviews
    interview_grace_before_minutes: int = 10
    interview_grace_after_minutes: int = 30  # how long after start the link still opens
    interview_no_show_minutes: int = 30
    interview_abandon_minutes: int = 15  # inactivity while in progress -> incomplete
    interview_max_minutes: int = 30
    interview_max_questions: int = 8
    interview_reminder_minutes: int = 30  # 0 disables reminders

    # Voice in the interview chat (Gnani). Voice controls are hidden when the key is empty.
    gnani_api_key: str = ""
    gnani_base_url: str = "https://api.vachana.ai"
    gnani_language: str = "en-IN"  # used for both speech-to-text and text-to-speech
    gnani_tts_model: str = "timbre-v2.5"
    gnani_tts_voice: str = "Kaveri"  # must belong to gnani_tts_model's voice catalog
    gnani_timeout_seconds: float = 30.0

    # Scheduler
    run_scheduler: bool = True

    @model_validator(mode="after")
    def _fallbacks(self):
        if not self.briefing_model:
            self.briefing_model = self.outreach_model
        if not self.summary_model:
            self.summary_model = self.interview_model
        return self

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.email_from)

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_username)

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)

    @property
    def voice_configured(self) -> bool:
        return bool(self.gnani_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
