import argparse
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from .config import Settings
from .server import create_app


def main():
    parser = argparse.ArgumentParser(description="Run a local mock WhatsApp conversation")
    parser.add_argument("--db", default="data/demo.db")
    args = parser.parse_args()
    settings = Settings(mode="mock", database_path=Path(args.db), media_dir=Path("data/demo-photos"))
    app = create_app(settings)
    client = TestClient(app)
    sender = "whatsapp:+15551234567"
    run_id = uuid4().hex[:8]
    samples = [
        {"MessageSid": f"demo-photo-{run_id}", "From": sender, "Body": "", "NumMedia": "1", "MediaUrl0": "mock://sink", "MediaContentType0": "image/png"},
        {"MessageSid": f"demo-followup-{run_id}", "From": sender, "Body": "What might it cost?", "NumMedia": "0"},
    ]
    for sample in samples:
        response = client.post("/whatsapp", data=sample)
        print(f"IN: {sample['Body'] or '[broken sink photo]'}")
        print(f"WEBHOOK: {response.status_code} {response.text}")
        with sqlite3.connect(args.db) as db:
            row = db.execute("SELECT body FROM messages WHERE direction='out' ORDER BY id DESC LIMIT 1").fetchone()
            print(f"OUT (mock, not sent): {row[0] if row else '[none]'}\n")


if __name__ == "__main__":
    main()
