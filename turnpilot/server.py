import logging
import re
import base64
import binascii
import secrets
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from .agents import follow_up
from .config import Settings
from .db import Store
from .media import download_image
from .workflow import inspect


log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate()
    store = Store(settings.database_path)
    app = FastAPI(title="TurnPilot AI", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store

    def send(sender: str, case_id: int | None, body: str):
        status = "mock_not_sent" if settings.mode == "mock" else "submitted_to_twilio"
        if settings.mode == "live":
            try:
                Client(settings.twilio_sid, settings.twilio_token).messages.create(
                    to=sender, from_=settings.twilio_from, body=body
                )
            except Exception:
                log.exception("WhatsApp send failed")
                status = "send_failed"
        store.outbound(case_id, sender, body, status)

    def process(message_id: int, sender: str, body: str, media_url: str, media_mime: str):
        case_id = None
        try:
            prior = store.active_case(sender)
            cancel_request = bool(re.search(
                r"^\s*(?:please\s+)?(?:cancel(?:\s+(?:it|this|my|the)\s*(?:draft|request|case)?)?|never\s?mind|i\s+want\s+to\s+cancel(?:\s+this)?)\s*[.!]?\s*$",
                body, re.I,
            ))
            if cancel_request and not media_url:
                if prior:
                    case_id = prior["id"]
                    store.attach(message_id, case_id)
                    store.cancel_case(case_id)
                    store.action(case_id, message_id, "bob", "cancelled_draft", {"case_id": case_id})
                    send(sender, case_id, "I'm Bob. I've cancelled this draft case. No vendor had been contacted or booked, and no payment was made. Send me a new photo or description whenever you need help.")
                else:
                    send(sender, None, "I'm Bob. There isn't an active draft case to cancel. Send me a photo or description whenever you need help.")
                return
            new_issue = bool(re.search(r"\b(new issue|another issue|separate issue)\b", body, re.I))
            property_match = re.search(r"(?:property|unit|address):\s*([^\n]{3,120})", body, re.I)
            property_context = property_match.group(1).strip() if property_match else ""
            if media_url:
                image = download_image(settings, media_url, media_mime, message_id)
            else:
                image = None
            if not image and prior and not new_issue:
                case_id = prior["id"]
                store.attach(message_id, case_id)
                if property_context:
                    store.set_property_context(case_id, property_context)
                    prior["property_context"] = property_context
                store.action(case_id, message_id, "conversation", "follow_up", {"question": body})
                reply = follow_up(settings, body, prior, store.history(case_id))
                send(sender, case_id, reply)
                return
            if not image and not re.search(r"sink|pipe|leak|water|drain|plumb|electric|outlet|wire|spark|clean|stain|mold|trash", body, re.I):
                send(sender, None, "Hi, I'm Bob. What happened, and where in the property? A clear photo helps me choose the right specialist. You can add 'Property: Unit 2' for context.")
                return
            case_id = prior["id"] if prior and image and not new_issue else store.create_case(sender, property_context)
            store.attach(message_id, case_id)
            if prior and case_id == prior["id"] and property_context:
                store.set_property_context(case_id, property_context)
            if image:
                store.photo(message_id, str(image[0]), image[1], media_url)
            prior_context = dict(prior) if prior and case_id == prior["id"] else None
            result = inspect(
                settings, body, image, prior_context,
                property_context or (prior or {}).get("property_context", ""),
                on_action=lambda agent, action, detail: store.action(case_id, message_id, agent, action, detail),
            )
            store.update_case(case_id, result.triage, result.specialist)
            send(sender, case_id, result.reply)
        except Exception as exc:
            log.exception("Message processing failed")
            store.action(case_id, message_id, "system", "error", {"error": str(exc)[:300]})
            send(sender, case_id, "I could not process that photo or message. Please try a clear JPEG, PNG, or WebP photo under 8 MB, or describe the issue in text.")

    @app.get("/health")
    def health():
        return {"ok": True, "mode": settings.mode}

    @app.post("/whatsapp", response_class=PlainTextResponse)
    async def whatsapp(request: Request, background_tasks: BackgroundTasks):
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
        if not settings.observer_password:
            raise HTTPException(status_code=404)
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

    return app


app = create_app()
