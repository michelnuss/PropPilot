import sqlite3
from dataclasses import replace
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
        openai_key="test" if mode in {"live", "demo"} else "",
        gemini_key="test" if mode in {"live", "demo"} and provider == "gemini" else "",
        observer_password="demo-password" if mode == "demo" else "",
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


def test_bob_cancels_only_the_draft_and_can_open_new_case(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM1", image=True)
    post(client, "SM2", "cancel the request")
    case = rows(settings.database_path, "cases")[0]
    assert case["status"] == "cancelled"
    assert rows(settings.database_path, "tasks")[0]["status"] == "cancelled"
    assert rows(settings.database_path, "agent_actions")[-1]["agent"] == "bob"
    assert "No vendor had been contacted or booked" in rows(settings.database_path, "messages")[-1]["body"]
    post(client, "SM3", "The kitchen sink is leaking")
    assert len(rows(settings.database_path, "cases")) == 2


def test_bob_asks_for_details_and_does_not_assign_renter_liability(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM1", "Something is broken")
    assert "What happened, and where" in rows(settings.database_path, "messages")[-1]["body"]
    assert not rows(settings.database_path, "cases")
    post(client, "SM2", image=True)
    post(client, "SM3", "How much will the renter pay for this?")
    answer = rows(settings.database_path, "messages")[-1]["body"]
    assert "No payment is requested" in answer
    assert "depends on the lease" in answer


def test_observer_shows_saved_handoffs_and_requires_password_in_live_mode(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM1", image=True)
    page = client.get("/observer")
    assert page.status_code == 200 and "Meet the crew" in page.text
    assert client.get("/observer/bob.png").status_code == 200
    state = client.get("/observer/state").json()
    assert [a["agent"] for a in state["actions"]][::-1] == ["inspection_triage", "dispatcher", "plumber_agent"]
    _, live_settings = setup(tmp_path, "live")
    live = replace(live_settings, observer_password="demo-secret")
    live_client = TestClient(create_app(live))
    assert live_client.get("/observer").status_code == 401
    assert live_client.get("/observer/state", auth=("demo", "wrong")).status_code == 401
    assert live_client.get("/observer/state", auth=("demo", "demo-secret")).status_code == 200
    assert TestClient(create_app(replace(live, observer_password=""))).get("/observer").status_code == 404


def test_browser_chat_uses_saved_photo_case_and_followup(tmp_path):
    client, settings = setup(tmp_path)
    assert client.get("/chat").status_code == 200
    assert "Bob" in client.get("/chat").text
    assert client.get("/chat/sample/leaking-sink.png").status_code == 200
    assert client.get("/chat/sample/unknown.png").status_code == 404
    photo = Path("demo/scenarios/leaking-sink.png").read_bytes()
    response = client.post("/chat/message", data={"body": "Property: Unit 2"}, files={"photo": ("sink.png", photo, "image/png")})
    assert response.status_code == 200
    assert "simulated Plumber Agent" in response.json()["reply"]
    messages = client.get("/chat/state").json()["messages"]
    assert len(messages) == 2
    assert messages[0]["photo_url"].startswith("/chat/photo/")
    assert client.get(messages[0]["photo_url"]).content == photo
    response = client.post("/chat/message", data={"body": "How urgent is it?"})
    assert "urgency" in response.json()["reply"]
    assert len(rows(settings.database_path, "cases")) == 1
    assert len(rows(settings.database_path, "agent_actions")) == 4
    other = TestClient(client.app)
    other.get("/chat")
    assert other.get("/chat/state").json()["messages"] == []
    assert other.get(messages[0]["photo_url"]).status_code == 404


def test_browser_chat_rejects_fake_images_and_demo_requires_password(tmp_path, monkeypatch):
    client, settings = setup(tmp_path)
    client.get("/chat")
    response = client.post("/chat/message", files={"photo": ("fake.png", b"not an image", "image/png")})
    assert response.status_code == 400
    assert rows(settings.database_path, "messages") == []
    demo, demo_settings = setup(tmp_path, "demo", "gemini")
    assert demo.get("/chat").status_code == 401
    assert demo.get("/chat", auth=("demo", "demo-password")).status_code == 200
    assert demo.get("/chat/state", auth=("demo", "demo-password")).status_code == 200
    assert demo.post("/whatsapp", data={"MessageSid": "x", "From": SENDER}).status_code == 404
    assert TestClient(create_app(replace(demo_settings, observer_password=""))).get("/chat").status_code == 404

    calls = []
    def fake_model(settings, instructions, text, image=None, *, schema, name):
        calls.append((name, bool(image)))
        if name == "inspection_triage":
            return {"trade": "electrician", "issue": "Broken outlet", "severity": "high", "visible_evidence": "Exposed wire", "uncertainty": "Needs an on-site check"}
        if name == "specialist_draft":
            return {"diagnosis": "Possible damaged outlet", "repair_plan": "Keep clear; qualified electrician to inspect", "estimate": "$200-$400", "slot": "Tomorrow 10 AM"}
        return {"answer": "Keep clear of the outlet. This is a simulated plan; no booking exists."}
    monkeypatch.setattr("turnpilot.agents._model_json", fake_model)
    photo = Path("demo/scenarios/damaged-outlet.png").read_bytes()
    response = demo.post("/chat/message", auth=("demo", "demo-password"), files={"photo": ("outlet.png", photo, "image/png")})
    assert response.status_code == 200
    assert "simulated Electrician Agent" in response.json()["reply"]
    response = demo.post("/chat/message", auth=("demo", "demo-password"), data={"body": "How urgent is it?"})
    assert response.status_code == 200
    assert calls == [("inspection_triage", True), ("specialist_draft", False), ("case_followup", False)]


def test_live_browser_preview_never_sends_twilio(tmp_path, monkeypatch):
    _, settings = setup(tmp_path, "live")
    settings = replace(settings, observer_password="demo-password")
    client = TestClient(create_app(settings))
    def unexpected_twilio(*args):
        raise AssertionError("Browser preview must not call Twilio")
    monkeypatch.setattr("turnpilot.server.Client", unexpected_twilio)
    assert client.get("/chat", auth=("demo", "demo-password")).status_code == 200
    response = client.post("/chat/message", auth=("demo", "demo-password"), data={"body": "Hello"})
    assert response.status_code == 200
    assert rows(settings.database_path, "messages")[-1]["status"] == "browser_demo"


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
    assert captured["json"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert len(captured["json"]["contents"][0]["parts"][1]["inline_data"]["data"]) > 1000
    assert captured["headers"]["x-goog-api-key"] == "test"


def test_gemini_retries_when_thinking_uses_output_limit(monkeypatch, tmp_path):
    limits = []

    class FakeResponse:
        def __init__(self, reason):
            self.reason = reason

        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"finishReason": self.reason, "content": {"parts": [{"text": '{"ok":true}'}]}}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            limits.append(kwargs["json"]["generationConfig"]["maxOutputTokens"])
            return FakeResponse("MAX_TOKENS" if len(limits) == 1 else "STOP")

    monkeypatch.setattr("turnpilot.agents.httpx.Client", FakeClient)
    _, settings = setup(tmp_path, "live", "gemini")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    assert _model_json(settings, "Inspect", "Question", schema=schema, name="check") == {"ok": True}
    assert limits == [4096, 8192]


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


def test_long_model_draft_keeps_simulation_disclosure(monkeypatch, tmp_path):
    _, settings = setup(tmp_path)
    monkeypatch.setattr("turnpilot.workflow.triage", lambda *args: {
        "trade": "plumber", "issue": "Leak " * 100, "uncertainty": "Unknown " * 100, "severity": "high"
    })
    monkeypatch.setattr("turnpilot.workflow.specialist", lambda *args: {
        "diagnosis": "Possible pipe damage " * 100,
        "repair_plan": "Inspect and replace fitting " * 100,
        "estimate": "$150–$350 " * 100,
        "slot": "Tomorrow morning " * 100,
    })
    reply = inspect(settings, "sink leak", None).reply
    assert len(reply) <= 1550
    assert "Connected to simulated Plumber Agent" in reply
    assert "No vendor has been contacted or booked" in reply


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
