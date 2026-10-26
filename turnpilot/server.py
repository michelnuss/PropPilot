import logging
import re

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from .agents import dispatch, follow_up, specialist, triage
from .config import Settings
from .db import Store
from .media import download_image


log = logging.getLogger(__name__)


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
                store.action(case_id, message_id, "conversation", "follow_up", {"question": body})
                reply = follow_up(settings, body, prior, store.history(case_id))
                send(sender, case_id, reply)
                return
            if not image and not re.search(r"sink|pipe|leak|water|drain|plumb|electric|outlet|wire|spark|clean|stain|mold|trash|broken|damage|repair", body, re.I):
                send(sender, None, "Hi! Send a photo of the property issue, or describe what needs attention. You can add 'Property: Unit 2' for context.")
                return
            case_id = prior["id"] if prior and image and not new_issue else store.create_case(sender, property_context)
            store.attach(message_id, case_id)
            if image:
                store.photo(message_id, str(image[0]), image[1], media_url)
            prior_context = dict(prior) if prior and case_id == prior["id"] else None
            triage_result = triage(settings, body, image, prior_context)
            store.action(case_id, message_id, "inspection_triage", "classified", triage_result)
            route = dispatch(triage_result)
            store.action(case_id, message_id, "dispatcher", "routed", route)
            plan = specialist(settings, triage_result, property_context or (prior or {}).get("property_context", ""))
            store.action(case_id, message_id, f"{route['route']}", "draft_plan", plan)
            store.update_case(case_id, triage_result, plan)
            intro = "[MOCK MODE] " if settings.mode == "mock" else ""
            caution = " If water is near electricity, keep clear and seek urgent qualified help." if triage_result["severity"] == "high" else ""
            reply = (
                f"{intro}I can see/report: {triage_result['issue']}. {triage_result['uncertainty']}\n"
                f"Connected to simulated {triage_result['trade'].title()} Agent. Draft repair request prepared.\n"
                f"Possible diagnosis: {plan['diagnosis']}\nPlan: {plan['repair_plan']}\n"
                f"Rough demo-only estimate: {plan['estimate']}. Simulated slot: {plan['slot']}.\n"
                f"No vendor has been contacted or booked.{caution} Ask me a follow-up question."
            )
            send(sender, case_id, reply[:1550])
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

    return app


app = create_app()
