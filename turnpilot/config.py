import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    mode: str = os.getenv("APP_MODE", "mock")
    provider: str = os.getenv("AI_PROVIDER", "gemini")
    database_path: Path = Path(os.getenv("DATABASE_PATH", "data/turnpilot.db"))
    media_dir: Path = Path(os.getenv("MEDIA_DIR", "data/photos"))
    twilio_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from: str = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    webhook_url: str = os.getenv("TWILIO_WEBHOOK_URL", "")
    openai_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    gemini_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

    def validate(self) -> None:
        if self.mode not in {"mock", "live"}:
            raise ValueError("APP_MODE must be mock or live")
        if self.provider not in {"gemini", "openai"}:
            raise ValueError("AI_PROVIDER must be gemini or openai")
        if self.mode == "live":
            if not all([self.twilio_sid, self.twilio_token, self.twilio_from, self.webhook_url]):
                raise ValueError("Live mode requires Twilio credentials and webhook URL")
            if self.provider == "gemini" and not self.gemini_key:
                raise ValueError("Live Gemini mode requires GEMINI_API_KEY")
            if self.provider == "openai" and not self.openai_key:
                raise ValueError("Live OpenAI mode requires OPENAI_API_KEY")
