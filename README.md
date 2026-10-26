# TurnPilot AI (PropPilot)

A WhatsApp-only hackathon demo for property inspection follow-up. A photo or text report goes through **inspection/triage → dispatcher → simulated trade specialist**. Replies and follow-up questions stay in WhatsApp. No real contractor is contacted, no booking is made, and estimates and slots are simulated.

## What is real and what is simulated

- **Live mode:** Twilio WhatsApp Sandbox receives actual messages and photos; the server downloads the photo, sends its bytes to a vision-capable OpenAI model, persists the case in SQLite, and replies through Twilio.
- **Mock mode:** runs locally without credentials. The `demo/broken_sink.png` fixture follows a deterministic plumber path. The reply says `[MOCK MODE]`, and outbound messages are saved to SQLite but **not sent to WhatsApp**. Mock mode does not analyze pixels.
- Both modes run separate triage, dispatcher, and specialist steps, recorded in `agent_actions`.

## Run locally and test without credentials

Use Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m pytest -q
python -m turnpilot.demo --db data/demo.db
uvicorn turnpilot.server:app --host 127.0.0.1 --port 8000
```

In another terminal, send the mock photo webhook:

```bash
curl -X POST http://127.0.0.1:8000/whatsapp \
  -d 'MessageSid=demo-live-1' --data-urlencode 'From=whatsapp:+15551234567' \
  -d 'Body=' -d 'NumMedia=1' -d 'MediaUrl0=mock://sink' \
  -d 'MediaContentType0=image/png'
```

Follow up with another `MessageSid`:

```bash
curl -X POST http://127.0.0.1:8000/whatsapp \
  -d 'MessageSid=demo-live-2' --data-urlencode 'From=whatsapp:+15551234567' \
  -d 'Body=What might it cost?' -d 'NumMedia=0'
```

The webhook returns quick TwiML acknowledgments. In mock mode, inspect the saved, unsent responses with:

```bash
sqlite3 data/turnpilot.db 'select id, direction, body, status from messages order by id;'
sqlite3 data/turnpilot.db 'select agent, action, detail_json from agent_actions order by id;'
```

`GET http://127.0.0.1:8000/dev/actions` is a minimal developer log endpoint available only in mock mode.

## Connect a real WhatsApp chat

1. Create a Twilio account and open **Messaging → Try it out → Send a WhatsApp message** (the WhatsApp Sandbox). Follow the console's current join instructions from your own WhatsApp number. Use the sandbox sender number displayed there.
2. Create an OpenAI API key with access to a vision-capable model. The default is `gpt-4.1-mini`; set `OPENAI_MODEL` if your account uses another supported model. Model use may incur charges.
3. Fill `.env` with `APP_MODE=live`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` (formatted `whatsapp:+...`), `OPENAI_API_KEY`, and `OPENAI_MODEL`. Never commit `.env`.
4. Start the server: `uvicorn turnpilot.server:app --host 0.0.0.0 --port 8000`.
5. Start a public test tunnel, for example `ngrok http 8000`. Set `TWILIO_WEBHOOK_URL` in `.env` to the tunnel's exact HTTPS URL plus `/whatsapp`, such as `https://example.ngrok-free.app/whatsapp`. Restart the server after editing `.env`.
6. In Twilio Sandbox settings, set **When a message comes in** to that same URL, with method **POST**, and save it. The exact URL is required for Twilio signature validation. If the tunnel URL changes, update both places and restart.
7. Send a text such as `Property: Unit 2. The kitchen sink pipe is leaking`, or send `demo/broken_sink.png` from the joined WhatsApp account. Wait for the acknowledgement and then the detailed response. Ask `How urgent is it?`, `What might it cost?`, and `What happens next?` in the same chat.

The tunnel exposes your local server temporarily; use it only for the test. The Twilio request signature is checked in live mode. The app requires only one image per message, up to 8 MB, in JPEG, PNG, or WebP. It stores downloaded photos under `data/photos/` and SQLite data under `data/turnpilot.db`; `data/` is ignored by Git.

### Troubleshooting

- **403 webhook response:** `TWILIO_WEBHOOK_URL` must exactly match the URL Twilio called, including HTTPS, host, path, and any query string; check `TWILIO_AUTH_TOKEN`.
- **Acknowledgement but no detailed reply:** check the server logs and `messages.status` in SQLite. The outbound REST call needs correct Twilio credentials, a joined sandbox sender, and an active WhatsApp test session.
- **Photo cannot be processed:** check that Twilio's `MediaUrl0` can be fetched with your account credentials and that the image is JPEG, PNG, or WebP under 8 MB.
- **Model call fails:** check `OPENAI_API_KEY`, model access, and available API billing. No response is labeled as a real analysis if processing fails.

The integration setup follows [Twilio's WhatsApp quickstart](https://www.twilio.com/docs/whatsapp/quickstart), [incoming media guidance](https://www.twilio.com/docs/whatsapp/tutorial/send-and-receive-media-messages-whatsapp-python), [request validation guidance](https://www.twilio.com/docs/usage/security), and [OpenAI's image input reference](https://platform.openai.com/docs/api-reference/responses).
