import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from turnpilot.agents import _model_json, dispatch, follow_up, triage
from turnpilot.config import Settings
from turnpilot.server import create_app
from turnpilot.workflow import inspect


SENDER = "whatsapp:+15551234567"


def setup(tmp_path, mode="mock", provider="openai"):
    settings = Settings(
        mode=mode,
        provider=provider,
        database_path=tmp_path / "test.db",
        media_dir=tmp_path / "photos",
        twilio_sid="ACtest" if mode == "live" else "",
        twilio_token="token" if mode == "live" else "",
        webhook_url="https://example.com/whatsapp" if mode == "live" else "",
        openai_key="test" if mode == "live" else "",
        gemini_key="test" if mode == "live" and provider == "gemini" else "",
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
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    assert _model_json(settings, "Inspect", "What is wrong?", (image, "image/png"), schema=schema, name="test_output") == {"ok": True}
    parts = captured["input"][0]["content"]
    assert parts[1]["type"] == "input_image"
    assert parts[1]["image_url"].startswith("data:image/png;base64,")
    assert len(parts[1]["image_url"]) > 1000
    assert captured["text"]["format"] == {"type": "json_schema", "name": "test_output", "schema": schema, "strict": True}


def test_gemini_vision_request_uses_inline_photo_and_json_schema(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"ok":true}'}]}}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr("turnpilot.agents.httpx.Client", FakeClient)
    _, settings = setup(tmp_path, "live", "gemini")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    result = _model_json(settings, "Inspect", "What is wrong?", (Path("demo/broken_sink.png"), "image/png"), schema=schema, name="test_output")
    assert result == {"ok": True}
    assert captured["url"].endswith("/gemini-3.8-flash:generateContent")
    assert captured["json"]["generationConfig"]["responseFormat"]["text"]["schema"] == schema
    assert len(captured["json"]["contents"][0]["parts"][1]["inline_data"]["data"]) > 1000
    assert captured["headers"]["x-goog-api-key"] == "test"


def test_mock_sparking_outlet_is_urgent(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM6", "The outlet sparks and there is an exposed wire")
    case = rows(settings.database_path, "cases")[0]
    assert case["trade"] == "electrician"
    assert case["severity"] == "high"
    assert "keep clear" in rows(settings.database_path, "messages")[-1]["body"]


def test_captioned_mock_photo_and_later_property_context(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM7", "Property: Unit 2", image=True)
    assert rows(settings.database_path, "cases")[0]["trade"] == "plumber"
    post(client, "SM8", "Property: Unit 3\nWhat happens next?")
    assert rows(settings.database_path, "cases")[0]["property_context"] == "Unit 3"
    assert "simulated Plumber Agent" in rows(settings.database_path, "messages")[-1]["body"]


def test_workflow_has_no_whatsapp_or_database_dependency(tmp_path):
    _, settings = setup(tmp_path)
    actions = []
    result = inspect(
        settings, "Property: Unit 2", (Path("demo/broken_sink.png"), "image/png"),
        property_context="Unit 2", on_action=lambda agent, action, detail: actions.append((agent, action)),
    )
    assert result.triage["trade"] == "plumber"
    assert result.dispatch["route"] == "plumber_agent"
    assert result.specialist["simulated"] is True
    assert actions == [("inspection_triage", "classified"), ("dispatcher", "routed"), ("plumber_agent", "draft_plan")]
    assert "No vendor has been contacted" in result.reply


def test_workflow_preserves_completed_steps_on_specialist_failure(monkeypatch, tmp_path):
    _, settings = setup(tmp_path)
    actions = []
    monkeypatch.setattr("turnpilot.workflow.specialist", lambda *args: (_ for _ in ()).throw(ValueError("model failed")))
    with pytest.raises(ValueError, match="model failed"):
        inspect(settings, "sink leak", None, on_action=lambda agent, action, detail: actions.append(agent))
    assert actions == ["inspection_triage", "dispatcher"]


def test_live_triage_elevates_reported_electrical_hazard(monkeypatch, tmp_path):
    _, settings = setup(tmp_path, "live")
    monkeypatch.setattr("turnpilot.agents._model_json", lambda *args, **kwargs: {
        "trade": "electrician", "issue": "Sparking outlet", "severity": "low",
        "visible_evidence": "Sparks at an outlet", "uncertainty": "Needs on-site confirmation",
    })
    result = triage(settings, "The outlet is sparking", None, None)
    assert result["severity"] == "high"


def test_live_followup_does_not_claim_a_real_booking(monkeypatch, tmp_path):
    _, settings = setup(tmp_path, "live")
    monkeypatch.setattr("turnpilot.agents._model_json", lambda *args, **kwargs: {"answer": "We booked a plumber for tomorrow."})
    case = {"trade": "plumber", "plan_json": "{}"}
    answer = follow_up(settings, "What happens next?", case, [])
    assert "No vendor has been contacted or booked" in answer
    assert "We booked" not in answer


def test_mock_electrician_followup_uses_electrical_advice(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM9", "The outlet sparks")
    post(client, "SM10", "How urgent is it?")
    reply = rows(settings.database_path, "messages")[-1]["body"]
    assert "qualified electrician" in reply
    assert "shut off the water" not in reply


def test_general_maintenance_route_has_stable_agent_name():
    assert dispatch({"trade": "general maintenance"})["route"] == "general_maintenance_agent"
