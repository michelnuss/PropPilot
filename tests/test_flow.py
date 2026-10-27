import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import httpx
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
    assert [a["agent"] for a in actions] == ["inspection_triage"]
    assert not rows(settings.database_path, "tasks")
    reply = rows(settings.database_path, "messages")[-1]["body"]
    assert "Want me to ask our demo plumber" in reply
    assert "[MOCK MODE]" in rows(settings.database_path, "messages")[-2]["body"]
    post(client, "SM2", "yes")
    actions = rows(settings.database_path, "agent_actions")
    assert [a["agent"] for a in actions] == ["inspection_triage", "bob", "dispatcher", "plumber_agent"]
    assert len(rows(settings.database_path, "tasks")) == 1
    replies = [m["body"] for m in rows(settings.database_path, "messages")[-3:]]
    assert "talking to our demo plumber" in replies[0]
    assert "17:00" in replies[1] and "about 2 hours" in replies[1]
    assert "owner would cover" in replies[2]
    assert "Of course" not in replies[2]  # phone sender has no demo name
    post(client, "SM3", "What might it cost?")
    followup = rows(settings.database_path, "messages")[-1]["body"]
    assert "$150–$350" in followup
    assert len(rows(settings.database_path, "cases")) == 1


def test_duplicate_sid_and_new_photo_updates_case(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM1", image=True)
    response = post(client, "SM1", image=True)
    assert "already received" in response.text
    assert len(rows(settings.database_path, "photos")) == 1
    assert not rows(settings.database_path, "tasks")
    post(client, "SM2", "Here is an update", image=True)
    assert len(rows(settings.database_path, "cases")) == 1
    assert len(rows(settings.database_path, "photos")) == 2
    assert not rows(settings.database_path, "tasks")
    post(client, "SM3", "yes")
    assert len(rows(settings.database_path, "tasks")) == 1


def test_bob_cancels_only_the_draft_and_can_open_new_case(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM1", image=True)
    post(client, "SM2", "yes")
    post(client, "SM3", "cancel the request")
    case = rows(settings.database_path, "cases")[0]
    assert case["status"] == "cancelled"
    assert rows(settings.database_path, "tasks")[0]["status"] == "cancelled"
    assert rows(settings.database_path, "agent_actions")[-1]["agent"] == "bob"
    assert "Nothing was booked" in rows(settings.database_path, "messages")[-1]["body"]
    post(client, "SM4", "The kitchen sink is leaking")
    assert len(rows(settings.database_path, "cases")) == 1
    post(client, "SM5", image=True)
    assert len(rows(settings.database_path, "cases")) == 2


def test_bob_asks_for_details_and_does_not_assign_renter_liability(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM1", "Something is broken")
    assert "send a clear photo" in rows(settings.database_path, "messages")[-1]["body"]
    assert not rows(settings.database_path, "cases")
    post(client, "SM2", image=True)
    post(client, "SM3", "How much will the renter pay for this?")
    answer = rows(settings.database_path, "messages")[-1]["body"]
    assert "owner would cover" in answer
    assert "guest-caused damage" in answer


def test_pending_photo_waits_for_casual_consent_and_does_not_invent_availability(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM1", image=True)
    post(client, "SM2", "How much would it cost?")
    assert "Want me to?" in rows(settings.database_path, "messages")[-1]["body"]
    assert not rows(settings.database_path, "tasks")
    post(client, "SM3", "yea")
    assert len(rows(settings.database_path, "tasks")) == 1
    assert "17:00" in rows(settings.database_path, "messages")[-2]["body"]


def test_observer_shows_saved_handoffs_and_requires_password_in_live_mode(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM1", image=True)
    post(client, "SM2", "yes")
    page = client.get("/observer")
    assert page.status_code == 200 and "Meet the crew" in page.text
    assert client.get("/observer/bob.png").status_code == 200
    state = client.get("/observer/state").json()
    assert [a["agent"] for a in state["actions"]][::-1] == ["inspection_triage", "bob", "dispatcher", "plumber_agent"]
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
    assert "Want me to ask our demo plumber" in response.json()["reply"]
    messages = client.get("/chat/state").json()["messages"]
    assert len(messages) == 3
    assert messages[0]["photo_url"].startswith("/chat/photo/")
    assert client.get(messages[0]["photo_url"]).content == photo
    assert "Want me to ask" in response.json()["reply"]
    response = client.post("/chat/message", data={"body": "yes"})
    assert "Would 17:00 work?" in response.json()["reply"]
    response = client.post("/chat/message", data={"body": "yes"})
    assert "need anything else?" in response.json()["reply"]
    response = client.post("/chat/message", data={"body": "How urgent is it?"})
    assert "urgency" in response.json()["reply"]
    assert len(rows(settings.database_path, "cases")) == 1
    assert len(rows(settings.database_path, "agent_actions")) == 6
    other = TestClient(client.app)
    other.get("/chat")
    assert other.get("/chat/state").json()["messages"] == []
    assert other.get(messages[0]["photo_url"]).status_code == 404
    assert client.post("/chat/new").status_code == 200
    assert client.get("/chat/state").json()["messages"] == []
    assert len(rows(settings.database_path, "messages")) > 0  # Restart keeps the old history in SQLite.


def test_new_issue_photo_does_not_inherit_old_case_description(tmp_path, monkeypatch):
    client, settings = setup(tmp_path)
    post(client, "SM1", "Kitchen sink leak", image=True)
    post(client, "SM2", "How urgent is that sink leak?")
    seen = []
    def cleaner_triage(settings, message, image, prior):
        seen.append((message, prior))
        return {"trade": "cleaner", "issue": "Dirty bathroom", "severity": "medium", "visible_evidence": "Towels and muddy floor", "uncertainty": "Check on site."}
    monkeypatch.setattr("turnpilot.workflow.triage", cleaner_triage)
    post(client, "SM3", "new issue", image=True)
    assert seen == [("new issue", None)]
    cases = rows(settings.database_path, "cases")
    assert len(cases) == 2 and cases[-1]["trade"] == "cleaner"


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
    assert "Want me to ask our demo electrician" in response.json()["reply"]
    response = demo.post("/chat/message", auth=("demo", "demo-password"), data={"body": "yes"})
    assert response.status_code == 200
    response = demo.post("/chat/message", auth=("demo", "demo-password"), data={"body": "How urgent is it?"})
    assert response.status_code == 200
    assert calls == [("inspection_triage", True), ("specialist_draft", False), ("case_followup", False)]


@pytest.mark.parametrize("image_name,trade,issue", [
    ("leaking-sink.png", "plumber", "a leaking kitchen pipe"),
    ("turnover-cleaning.png", "cleaner", "a bathroom needing a turnover clean"),
])
def test_gemini_conversation_waits_for_photo_then_stages_handoff(tmp_path, monkeypatch, image_name, trade, issue):
    client, settings = setup(tmp_path, "demo", "gemini")
    calls = []
    def fake_model(settings, instructions, text, image=None, *, schema, name):
        calls.append((name, bool(image)))
        if name == "bob_chat":
            return {"answer": "I can help with that. Please send a clear photo when you can."}
        if name == "inspection_triage":
            return {"trade": trade, "issue": issue, "severity": "medium", "visible_evidence": issue, "uncertainty": "An on-site check is needed."}
        if name == "specialist_draft":
            return {"diagnosis": f"Possible {issue}", "repair_plan": "Inspect the area and prepare the right repair or cleaning supplies.", "estimate": "$100-$300", "slot": "Tomorrow"}
        raise AssertionError(name)
    monkeypatch.setattr("turnpilot.agents._model_json", fake_model)
    auth = ("demo", "demo-password")
    client.get("/chat", auth=auth)
    greeting = client.post("/chat/message", auth=auth, data={"body": "Property: Unit 9\nCan you help?"})
    assert greeting.status_code == 200
    assert "Please send a clear photo" in greeting.json()["reply"]
    assert not rows(settings.database_path, "cases")
    assert not rows(settings.database_path, "tasks")
    photo = Path("demo/scenarios", image_name).read_bytes()
    response = client.post("/chat/message", auth=auth, files={"photo": (image_name, photo, "image/png")})
    assert response.status_code == 200
    messages = rows(settings.database_path, "messages")
    assert len(messages[-2]["body"]) < 140
    assert f"Want me to ask our demo {trade}" in messages[-1]["body"]
    assert not rows(settings.database_path, "tasks")
    response = client.post("/chat/message", auth=auth, data={"body": "yea"})
    assert response.status_code == 200
    messages = rows(settings.database_path, "messages")
    assert "talking to our demo" in messages[-3]["body"]
    assert "Just heard back" in messages[-2]["body"]
    assert ("17:00" if trade == "plumber" else "10:00") in messages[-2]["body"]
    assert "about 2 hours" in messages[-2]["body"]
    assert "Would " in messages[-1]["body"]
    if trade == "cleaner":
        assert "$100/hour" in messages[-2]["body"]
    else:
        assert "owner would cover" in messages[-1]["body"]
    response = client.post("/chat/message", auth=auth, data={"body": "yes"})
    assert "Of course, Michel" in response.json()["reply"]
    case = rows(settings.database_path, "cases")[0]
    assert case["trade"] == trade and case["property_context"] == "Unit 9"
    assert calls == [("bob_chat", False), ("inspection_triage", True), ("specialist_draft", False)]


def test_live_browser_preview_never_sends_twilio(tmp_path, monkeypatch):
    _, settings = setup(tmp_path, "live")
    settings = replace(settings, observer_password="demo-password")
    client = TestClient(create_app(settings))
    def unexpected_twilio(*args):
        raise AssertionError("Browser preview must not call Twilio")
    monkeypatch.setattr("turnpilot.server.Client", unexpected_twilio)
    monkeypatch.setattr("turnpilot.server.bob_chat", lambda *args: "Hi, please send a photo.")
    assert client.get("/chat", auth=("demo", "demo-password")).status_code == 200
    response = client.post("/chat/message", auth=("demo", "demo-password"), data={"body": "Hello"})
    assert response.status_code == 200
    assert rows(settings.database_path, "messages")[-1]["status"] == "browser_demo"


def test_local_demo_opens_without_basic_but_forwarded_request_needs_it(tmp_path):
    _, settings = setup(tmp_path, "demo", "gemini")
    app = create_app(settings)
    async def check():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45500))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as client:
            assert (await client.get("/chat")).status_code == 200
            assert (await client.get("/chat/state")).status_code == 200
            assert (await client.get("/observer")).status_code == 200
            assert (await client.get("/chat", headers={"X-Forwarded-For": "203.0.113.5"})).status_code == 401
    asyncio.run(check())


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
    assert not rows(settings.database_path, "cases")
    assert "keep clear" in rows(settings.database_path, "messages")[-1]["body"].lower()
    assert "qualified electrician" in rows(settings.database_path, "messages")[-1]["body"]


def test_captioned_mock_photo_and_later_property_context(tmp_path):
    client, settings = setup(tmp_path)
    post(client, "SM7", "Property: Unit 2", image=True)
    assert rows(settings.database_path, "cases")[0]["trade"] == "plumber"
    post(client, "SM8", "Property: Unit 3\nWhat happens next?")
    assert rows(settings.database_path, "cases")[0]["property_context"] == "Unit 3"
    assert "demo plumber" in rows(settings.database_path, "messages")[-1]["body"]


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
    assert "No real contractor has been contacted" in result.reply


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
    assert "simulated Plumber Agent" in reply
    assert "No real contractor has been contacted or booked" in reply


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
    assert not rows(settings.database_path, "cases")


def test_general_maintenance_route_has_stable_agent_name():
    assert dispatch({"trade": "general maintenance"})["route"] == "general_maintenance_agent"
