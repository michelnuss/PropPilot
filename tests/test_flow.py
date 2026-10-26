import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from turnpilot.agents import _model_json
from turnpilot.config import Settings
from turnpilot.server import create_app


SENDER = "whatsapp:+15551234567"


def setup(tmp_path, mode="mock"):
    settings = Settings(
        mode=mode,
        database_path=tmp_path / "test.db",
        media_dir=tmp_path / "photos",
        twilio_sid="ACtest" if mode == "live" else "",
        twilio_token="token" if mode == "live" else "",
        webhook_url="https://example.com/whatsapp" if mode == "live" else "",
        openai_key="test" if mode == "live" else "",
    )
    return TestClient(create_app(settings)), settings


def post(client, sid, body="", image=False):
    data = {"MessageSid": sid, "From": SENDER, "Body": body, "NumMedia": "1" if image else "0"}
    if image:
        data.update({"MediaUrl0": "mock://sink", "MediaContentType0": "image/png"})
    return client.post("/whatsapp", data=data)


def rows(path, table):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_photo_routes_persists_and_followup(tmp_path):
    client, settings = setup(tmp_path)
    response = post(client, "SM1", image=True)
    assert response.status_code == 200
    assert "Photo received" in response.text
    cases = rows(settings.database_path, "cases")
    assert len(cases) == 1 and cases[0]["trade"] == "plumber"
    assert len(rows(settings.database_path, "photos")) == 1
    assert Path(rows(settings.database_path, "photos")[0]["path"]).exists()
    actions = rows(settings.database_path, "agent_actions")
    assert [a["agent"] for a in actions] == ["inspection_triage", "dispatcher", "plumber_agent"]
    assert len(rows(settings.database_path, "tasks")) == 1
    reply = rows(settings.database_path, "messages")[-1]["body"]
    assert "Connected to simulated Plumber Agent" in reply
    assert "No vendor has been contacted or booked" in reply
    assert "[MOCK MODE]" in reply
    post(client, "SM2", "What might it cost?")
    followup = rows(settings.database_path, "messages")[-1]["body"]
    assert "$150–$350" in followup
    assert len(rows(settings.database_path, "cases")) == 1


def test_duplicate_sid_and_new_photo_updates_case(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM1", image=True)
    response = post(client, "SM1", image=True)
    assert "already received" in response.text
    assert len(rows(settings.database_path, "photos")) == 1
    assert len(rows(settings.database_path, "tasks")) == 1
    post(client, "SM2", "Here is an update", image=True)
    assert len(rows(settings.database_path, "cases")) == 1
    assert len(rows(settings.database_path, "photos")) == 2
    assert len(rows(settings.database_path, "tasks")) == 2


def test_bad_media_is_logged_and_reported(tmp_path):
    client, settings = setup(tmp_path)
    data = {"MessageSid": "SM3", "From": SENDER, "Body": "", "NumMedia": "1", "MediaUrl0": "mock://sink", "MediaContentType0": "image/gif"}
    assert client.post("/whatsapp", data=data).status_code == 200
    assert "could not process" in rows(settings.database_path, "messages")[-1]["body"]
    assert rows(settings.database_path, "agent_actions")[-1]["action"] == "error"


def test_live_mode_rejects_unsigned_webhook(tmp_path):
    client, settings = setup(tmp_path, "live")
    response = post(client, "SM4", "sink broken")
    assert response.status_code == 403
    assert not rows(settings.database_path, "messages")


def test_live_mode_accepts_signed_webhook_and_sends_reply(monkeypatch, tmp_path):
    client, settings = setup(tmp_path, "live")
    sent = []

    class FakeMessages:
        def create(self, **kwargs):
            sent.append(kwargs)

    class FakeTwilio:
        def __init__(self, *args):
            self.messages = FakeMessages()

    monkeypatch.setattr("turnpilot.server.Client", FakeTwilio)
    data = {"MessageSid": "SM5", "From": SENDER, "Body": "hello", "NumMedia": "0"}
    signature = RequestValidator(settings.twilio_token).compute_signature(settings.webhook_url, data)
    response = client.post("/whatsapp", data=data, headers={"X-Twilio-Signature": signature})
    assert response.status_code == 200
    assert sent[0]["to"] == SENDER
    assert sent[0]["from_"] == settings.twilio_from
    assert rows(settings.database_path, "messages")[-1]["status"] == "submitted_to_twilio"


def test_vision_request_contains_real_image_bytes(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"output": [{"content": [{"type": "output_text", "text": '{"ok":true}'}]}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr("turnpilot.agents.httpx.Client", FakeClient)
    client, settings = setup(tmp_path, "live")
    image = Path("demo/broken_sink.png")
    assert _model_json(settings, "Inspect", "What is wrong?", (image, "image/png")) == {"ok": True}
    parts = captured["input"][0]["content"]
    assert parts[1]["type"] == "input_image"
    assert parts[1]["image_url"].startswith("data:image/png;base64,")
    assert len(parts[1]["image_url"]) > 1000


def test_mock_sparking_outlet_is_urgent(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM6", "The outlet sparks and there is an exposed wire")
    case = rows(settings.database_path, "cases")[0]
    assert case["trade"] == "electrician"
    assert case["severity"] == "high"
    assert "keep clear" in rows(settings.database_path, "messages")[-1]["body"]
