import logging
import re
import base64
import binascii
import secrets
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from .agents import bob_chat, follow_up
from .config import Settings
from .db import Store
from .media import MAX_BYTES, download_image, save_image, validate_image
from .workflow import assess_photo, prepare_handoff


log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
SAMPLE_DIR = Path(__file__).parent.parent / "demo" / "scenarios"
SAMPLE_IMAGES = {"leaking-sink.png", "damaged-outlet.png", "broken-window.png", "turnover-cleaning.png"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate()
    store = Store(settings.database_path)
    app = FastAPI(title="TurnPilot AI", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store

    def send(sender: str, case_id: int | None, body: str):
        is_whatsapp = sender.startswith("whatsapp:+")
        status = "submitted_to_twilio" if settings.mode == "live" and is_whatsapp else ("mock_not_sent" if settings.mode == "mock" else "browser_demo")
        if settings.mode == "live" and is_whatsapp:
            try:
                Client(settings.twilio_sid, settings.twilio_token).messages.create(
                    to=sender, from_=settings.twilio_from, body=body
                )
            except Exception:
                log.exception("WhatsApp send failed")
                status = "send_failed"
        store.outbound(case_id, sender, body, status)

    def process(message_id: int, sender: str, body: str, media_url: str = "", media_mime: str = "", uploaded_image=None):
        case_id = None
        try:
            prior = store.active_case(sender)
            cancel_request = bool(re.search(
                r"^\s*(?:please\s+)?(?:cancel(?:\s+(?:it|this|my|the)\s*(?:draft|request|case)?)?|never\s?mind|i\s+want\s+to\s+cancel(?:\s+this)?)\s*[.!]?\s*$",
                body, re.I,
            ))
            if cancel_request and not media_url and not uploaded_image:
                if prior:
                    case_id = prior["id"]
                    store.attach(message_id, case_id)
                    store.cancel_case(case_id)
                    store.action(case_id, message_id, "bob", "cancelled_draft", {"case_id": case_id})
                    reply = "Done — I closed that demo request. Nothing was booked. Need anything else?"
                else:
                    reply = "There's no open request to cancel. Send me a photo whenever you need help."
                send(sender, case_id, reply)
                return reply
            new_issue = bool(re.search(r"\b(new issue|another issue|separate issue)\b", body, re.I))
            property_match = re.search(r"(?:property|unit|address):\s*([^\n]{3,120})", body, re.I)
            property_context = property_match.group(1).strip() if property_match else ""
            if uploaded_image:
                image = uploaded_image
            elif media_url:
                image = download_image(settings, media_url, media_mime, message_id)
            else:
                image = None
            if image and prior and prior["plan_json"] != "{}" and not re.search(r"\b(update|same issue|more photos?|additional photo)\b", body, re.I):
                new_issue = True
            if not image and prior and store.pending_handoff(prior["id"]):
                affirmative = bool(re.match(r"^\s*(?:yes|yea|yeah|yep|yup|ya|sure|absolutely|sounds good|ok(?:ay)?|please do|go ahead|do it)(?:\s+(?:please|go ahead|do it))?\s*[.!]?\s*$", body, re.I))
                decline = bool(re.match(r"^\s*(?:no|no thanks|not yet|maybe later)\s*[.!]?\s*$", body, re.I))
                if affirmative:
                    case_id = prior["id"]
                    store.attach(message_id, case_id)
                    triage_result = store.last_triage(case_id)
                    if not triage_result:
                        raise ValueError("No saved inspection for pending handoff")
                    trade = triage_result["trade"].title()
                    store.action(case_id, message_id, "bob", "handoff_approved", {"trade": trade, "simulated": True})
                    helper = "cleaner" if triage_result["trade"] == "cleaner" else ("plumber" if triage_result["trade"] == "plumber" else trade.lower())
                    send(sender, case_id, f"One sec — I'm talking to our demo {helper} now.")
                    route, plan = prepare_handoff(
                        settings, triage_result, prior["property_context"],
                        on_action=lambda agent, action, detail: store.action(case_id, message_id, agent, action, detail),
                    )
                    store.update_case(case_id, triage_result, plan)
                    slot = plan["slot"].split(" (", 1)[0].lower()
                    duration = plan.get("duration", "about 2 hours").split(" (", 1)[0].lower()
                    if triage_result["trade"] == "cleaner":
                        send(sender, case_id, f"Just heard back from her. {slot.capitalize()} could work, and it'd take {duration} at $100/hour.")
                    elif triage_result["trade"] == "plumber":
                        send(sender, case_id, f"Just heard back from him. {slot.capitalize()} could work, and the repair should take {duration}.")
                    else:
                        send(sender, case_id, f"Just heard back. {slot.capitalize()} could work, and the job may take {duration}.")
                    if triage_result["trade"] == "cleaner":
                        reply = "Would 10:00 work for you?"
                    elif triage_result["trade"] == "plumber":
                        reply = f"For normal wear and tear, the owner would cover this repair. Rough demo cost: {plan['estimate'][:45]}. Would 17:00 work?"
                    else:
                        reply = f"Rough demo cost: {plan['estimate'][:45]}. Would that time work?"
                    send(sender, case_id, reply)
                    return reply
                if decline:
                    case_id = prior["id"]
                    store.attach(message_id, case_id)
                    reply = "No problem. Just say yes if you'd like me to check later."
                    send(sender, case_id, reply)
                    return reply
                if not new_issue and not re.search(r"\b(house is a mess|place is a mess|needs cleaning|dirty)\b", body, re.I):
                    case_id = prior["id"]
                    store.attach(message_id, case_id)
                    if property_context:
                        store.set_property_context(case_id, property_context)
                    trade = prior["trade"].lower()
                    if re.search(r"who pays|tenant pay|renter pay|owner pay|landlord pay|responsib|charge", body, re.I):
                        reply = ("For normal plumbing wear and tear, the owner would cover it. Unusual guest-caused damage needs the owner's review."
                                 if trade == "plumber" else "I'd put this on the owner's side for now. Unusual guest-caused damage needs a separate review.")
                    elif re.search(r"urgent|urgency|emergency|how soon", body, re.I):
                        reply = ("Please keep clear of the wiring and get urgent qualified help." if trade == "electrician" and prior["severity"] == "high"
                                 else "If it is getting worse or feels unsafe, keep clear and get qualified help. I can still ask our demo specialist about a time.")
                    else:
                        reply = f"I can ask our demo {trade} about the time and price. Want me to?"
                    send(sender, case_id, reply)
                    return reply
            if not image and prior and re.search(r"\b(house is a mess|place is a mess|needs cleaning|dirty|another issue|new issue)\b", body, re.I):
                new_issue = True
            if not image and prior and not new_issue:
                case_id = prior["id"]
                store.attach(message_id, case_id)
                last_outbound = store.last_outbound(case_id)
                if re.match(r"^\s*(?:yes|yea|yeah|yep|yup|ya|sure|ok(?:ay)?|that works|sounds good)\s*[.!]?\s*$", body, re.I) and last_outbound and re.search(r"\bwork(?: for you)?\?", last_outbound, re.I):
                    name = "Michel" if sender.startswith("browser:") and settings.mode == "demo" else ""
                    closing = f"Of course, {name}—need anything else?" if name else "Of course—need anything else?"
                    reply = f"Got it, I've saved that as the demo time. {closing}"
                    store.action(case_id, message_id, "bob", "demo_time_acknowledged", {"simulated": True})
                    send(sender, case_id, reply)
                    return reply
                if property_context:
                    store.set_property_context(case_id, property_context)
                    prior["property_context"] = property_context
                store.action(case_id, message_id, "conversation", "follow_up", {"question": body})
                reply = follow_up(settings, body, prior, store.history(case_id))
                send(sender, case_id, reply)
                return reply
            if not image:
                store.action(None, message_id, "bob", "general_chat", {"message": body[:300]})
                reply = bob_chat(settings, body, [] if new_issue else store.sender_history(sender))
                send(sender, None, reply)
                return reply
            earlier_messages = [] if new_issue else [
                item["body"] for item in store.sender_history(sender)
                if item["direction"] == "in" and item["id"] != message_id and item["body"]
            ]
            if not property_context:
                for earlier in reversed(earlier_messages):
                    earlier_property = re.search(r"(?:property|unit|address):\s*([^\n]{3,120})", earlier, re.I)
                    if earlier_property:
                        property_context = earlier_property.group(1).strip()
                        break
            case_id = prior["id"] if prior and image and not new_issue else store.create_case(sender, property_context)
            store.attach(message_id, case_id)
            if prior and case_id == prior["id"] and property_context:
                store.set_property_context(case_id, property_context)
            if image:
                store.photo(message_id, str(image[0]), image[1], media_url or "browser-upload")
            prior_context = dict(prior) if prior and case_id == prior["id"] else None
            triage_result = assess_photo(
                settings, body + ("\nEarlier messages: " + " | ".join(earlier_messages[-3:]) if earlier_messages else ""), image, prior_context,
                on_action=lambda agent, action, detail: store.action(case_id, message_id, agent, action, detail),
            )
            store.update_triage(case_id, triage_result)
            intro = "[MOCK MODE] " if settings.mode == "mock" else ""
            trade = triage_result["trade"]
            issue = triage_result["issue"].lower()
            if trade == "cleaner":
                observation = "That looks like a cleaning job. I can see why you'd want help with it."
            elif trade == "plumber" and "sink" in issue:
                observation = "That looks like a leak under the sink."
            elif trade == "plumber":
                observation = "That looks like a plumbing issue."
            elif trade == "electrician":
                observation = "That looks like an electrical issue."
            else:
                observation = "I can see why that needs attention."
            first = f"{intro}{observation}"
            if triage_result["severity"] == "high" and triage_result["trade"] == "plumber":
                first += " If water is running, shut it off if you can do that safely."
            elif triage_result["severity"] == "high" and triage_result["trade"] == "electrician":
                first += " Please keep clear of the wiring and seek urgent qualified help."
            send(sender, case_id, first)
            helper = "cleaner" if trade == "cleaner" else ("plumber" if trade == "plumber" else trade)
            reply = f"Want me to ask our demo {helper} if they can come by?"
            send(sender, case_id, reply)
            return reply
        except Exception as exc:
            log.exception("Message processing failed")
            store.action(case_id, message_id, "system", "error", {"error": str(exc)[:300]})
            reply = "I could not process that photo or message. Please try a clear JPEG, PNG, or WebP photo under 8 MB, or describe the issue in text."
            send(sender, case_id, reply)
            return reply

    @app.get("/health")
    def health():
        return {"ok": True, "mode": settings.mode}

    @app.post("/whatsapp", response_class=PlainTextResponse)
    async def whatsapp(request: Request, background_tasks: BackgroundTasks):
        if settings.mode == "demo":
            raise HTTPException(status_code=404)
        form = dict(await request.form())
        if settings.mode == "live":
            signature = request.headers.get("X-Twilio-Signature", "")
            if not RequestValidator(settings.twilio_token).validate(settings.webhook_url, form, signature):
                raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        sender = str(form.get("From", ""))
        sid = str(form.get("MessageSid", ""))
        if not sender.startswith("whatsapp:+") or not sid:
            raise HTTPException(status_code=400, detail="Missing WhatsApp sender or MessageSid")
        body = str(form.get("Body", ""))[:3000]
        try:
            media_count = int(form.get("NumMedia", "0"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid NumMedia")
        if media_count < 0:
            raise HTTPException(status_code=400, detail="Invalid NumMedia")
        if media_count > 1:
            raise HTTPException(status_code=400, detail="Send one image at a time")
        media_url = str(form.get("MediaUrl0", "")) if media_count else ""
        media_mime = str(form.get("MediaContentType0", "")) if media_count else ""
        if media_count and (not media_url or not media_mime):
            raise HTTPException(status_code=400, detail="Missing image metadata")
        message_id, is_new = store.inbound(sid, sender, body)
        response = MessagingResponse()
        if is_new:
            background_tasks.add_task(process, message_id, sender, body, media_url, media_mime)
            response.message("Photo received; I’m checking it now." if media_count else "Message received; I’m checking it now.")
        else:
            response.message("I already received this message.")
        return PlainTextResponse(str(response), media_type="application/xml")

    @app.get("/dev/actions")
    def actions():
        if settings.mode != "mock":
            raise HTTPException(status_code=404)
        return store.actions()

    def check_observer(request: Request):
        if settings.mode == "mock":
            return
        # The pitch preview is bound to loopback. In demo mode, let a local
        # browser open it without HTTP Basic, which some embedded browsers
        # cannot negotiate. Forwarded/tunnel requests still need a password.
        local_demo = (
            settings.mode == "demo"
            and request.client is not None
            and request.client.host in {"127.0.0.1", "::1"}
            and request.url.hostname in {"127.0.0.1", "localhost"}
            and not any(request.headers.get(name) for name in (
                "forwarded", "x-forwarded-for", "x-forwarded-host", "x-original-host"
            ))
        )
        if local_demo:
            return
        if not settings.observer_password:
            raise HTTPException(status_code=404, detail="Set OBSERVER_PASSWORD to use founder pages in AI mode")
        auth = request.headers.get("Authorization", "")
        try:
            scheme, encoded = auth.split(" ", 1)
            credentials = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, password = credentials.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            scheme, username, password = "", "", ""
        if not (scheme.lower() == "basic" and secrets.compare_digest(username, "demo")
                and secrets.compare_digest(password, settings.observer_password)):
            raise HTTPException(status_code=401, headers={"WWW-Authenticate": 'Basic realm="TurnPilot observer"'})

    @app.get("/observer")
    def observer(request: Request):
        check_observer(request)
        return FileResponse(STATIC_DIR / "observer.html", media_type="text/html")

    @app.get("/observer/state")
    def observer_state(request: Request):
        check_observer(request)
        return store.observer_state()

    @app.get("/observer/bob.png")
    def observer_bob(request: Request):
        check_observer(request)
        return FileResponse(STATIC_DIR / "bob.png", media_type="image/png")

    def browser_sender(request: Request):
        check_observer(request)
        token = request.cookies.get("turnpilot_chat", "")
        if len(token) != 48 or not all(char in "0123456789abcdef" for char in token):
            raise HTTPException(status_code=401, detail="Open /chat to start a browser session")
        return f"browser:{token}"

    @app.get("/chat")
    def chat(request: Request):
        check_observer(request)
        response = FileResponse(STATIC_DIR / "chat.html", media_type="text/html")
        token = request.cookies.get("turnpilot_chat", "")
        if len(token) != 48 or not all(char in "0123456789abcdef" for char in token):
            response.set_cookie("turnpilot_chat", secrets.token_hex(24), httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return response

    @app.post("/chat/new")
    def chat_new(request: Request):
        check_observer(request)
        response = JSONResponse({"ok": True})
        response.set_cookie("turnpilot_chat", secrets.token_hex(24), httponly=True,
                            samesite="strict", secure=request.url.scheme == "https")
        return response

    @app.get("/chat/bob.png")
    def chat_bob(request: Request):
        browser_sender(request)
        return FileResponse(STATIC_DIR / "bob.png", media_type="image/png")

    @app.get("/chat/state")
    def chat_state(request: Request):
        sender = browser_sender(request)
        messages = store.chat_messages(sender)
        for message in messages:
            if message["photo_id"]:
                message["photo_url"] = f"/chat/photo/{message['photo_id']}"
        active = store.active_case(sender)
        return {"mode": settings.mode, "provider": settings.provider, "active_case": active["id"] if active else None, "messages": messages}

    @app.get("/chat/photo/{photo_id}")
    def chat_photo(photo_id: int, request: Request):
        sender = browser_sender(request)
        photo = store.chat_photo(photo_id, sender)
        if not photo or not Path(photo["path"]).is_file():
            raise HTTPException(status_code=404)
        return FileResponse(photo["path"], media_type=photo["mime"])

    @app.get("/chat/sample/{name}")
    def chat_sample(name: str, request: Request):
        browser_sender(request)
        if name not in SAMPLE_IMAGES:
            raise HTTPException(status_code=404)
        return FileResponse(SAMPLE_DIR / name, media_type="image/png")

    @app.post("/chat/message")
    async def chat_message(request: Request, body: str = Form(""), photo: UploadFile | None = File(None)):
        sender = browser_sender(request)
        body = body.strip()[:3000]
        if not body and not photo:
            raise HTTPException(status_code=400, detail="Write a message or attach a photo")
        image_data = None
        image_mime = ""
        if photo:
            image_data = await photo.read(MAX_BYTES + 1)
            try:
                image_mime = validate_image(image_data, photo.content_type or "")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        message_id, _ = store.inbound(f"browser-{uuid.uuid4().hex}", sender, body)
        image = save_image(settings, image_data, image_mime, message_id) if image_data is not None else None
        reply = await run_in_threadpool(process, message_id, sender, body, "", "", image)
        return {"reply": reply, "message_id": message_id}

    return app


app = create_app()
