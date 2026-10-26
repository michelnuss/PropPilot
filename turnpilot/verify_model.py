"""One-command vision check after a founder configures a model key locally."""

from dataclasses import replace
from pathlib import Path

from .agents import triage
from .config import Settings


def main():
    settings = Settings()
    if settings.provider == "gemini" and not settings.gemini_key:
        raise SystemExit("Set GEMINI_API_KEY in .env before running the live vision check")
    if settings.provider == "openai" and not settings.openai_key:
        raise SystemExit("Set OPENAI_API_KEY in .env before running the live vision check")
    settings = replace(settings, mode="live")
    image = Path(__file__).parent.parent / "demo" / "broken_sink.png"
    result = triage(settings, "Kitchen sink inspection photo", (image, "image/png"), None)
    print(f"Provider: {settings.provider}; model: {settings.gemini_model if settings.provider == 'gemini' else settings.openai_model}")
    print(f"Trade: {result['trade']}; severity: {result['severity']}")
    print(f"Visible evidence: {result['visible_evidence']}")
    print(f"Uncertainty: {result['uncertainty']}")


if __name__ == "__main__":
    main()
