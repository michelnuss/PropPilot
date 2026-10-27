# Founder handoff: TurnPilot AI hackathon MVP

This document is for both founders and their coding agents. Read it with [PR #1](https://github.com/gabriellaec/PropPilot/pull/1) before integrating. The repository's `main` branch had only `# PropPilot` when Founder 2 began; this PR is a working implementation on a fork, not a change already merged into `main`.

## Ownership

| Founder | Primary responsibility | Current status |
| --- | --- | --- |
| Gabi, Founder 1 | WhatsApp Sandbox, webhook, media transport, public tunnel, credentials, live phone test, deployment/runtime | Her implementation is not yet visible on `main`; coordinate before replacing an existing adapter. |
| Michel, Founder 2 | Vision triage, dispatcher, trade specialist agents, follow-up reasoning, demo scenario, agent action evidence | Implemented in this PR, with a provisional Twilio adapter and SQLite store so the whole flow can be tested. |

The provisional Twilio adapter in `turnpilot/server.py` exists to make Founder 2's workflow runnable end to end. Gabi can reuse it or connect her own adapter to the agent functions; please avoid building two competing inbound webhook flows in the merged app. Bob is the friendly homeowner-facing persona; the dispatcher and specialists remain internal simulated software agents. The server now keeps ordinary text conversational and starts a specialist handoff only after a photo.

## Integration contract for Gabi's coding agent

The preferred integration entry points are in `turnpilot/workflow.py`:

- `assess_photo(settings, message, image, prior_case=None, on_action=None) -> dict` runs image triage and records the decision. Bob asks permission before any simulated specialist handoff.
- `prepare_handoff(settings, triage_result, property_context="", on_action=None) -> (route, plan)` runs dispatcher → specialist **only after the user's yes**. The server retrieves the last stored triage result and persists each action.
- `inspect(...) -> InspectionResult` remains as a combined transport-independent helper for integration or standalone tests, but the chat server uses the two-phase functions above to keep the conversation natural.

The lower-level agent functions remain available in `turnpilot/agents.py`:

- `triage(settings, message, image, prior) -> dict`: `image` is `(Path, MIME type)` after the server has downloaded the photo. It returns `trade`, `issue`, `severity`, `visible_evidence`, and `uncertainty`. In live mode it sends actual image bytes to a vision-capable model; in mock mode it follows the staged sink fixture and labels the result as mock.
- `dispatch(triage_result) -> dict`: chooses the specialist route and marks it simulated.
- `specialist(settings, triage_result, property_context) -> dict`: produces `diagnosis`, `repair_plan`, `estimate`, `slot`, and `simulated=True`. It does not contact anyone.
- `follow_up(settings, question, case, history) -> str`: answers using the stored case and recent messages. The case has `plan_json`, `issue`, `trade`, `severity`, and `property_context`.
- `bob_chat(settings, message, history) -> str`: uses Gemini for human-sounding text replies before a photo exists. It does not dispatch or claim vendor contact.

`turnpilot/server.py` currently calls `assess_photo(...)` after an image and `prepare_handoff(...)` after consent, uses callbacks to log each step in SQLite via `turnpilot/db.py`, and sends the replies through Twilio. The HTTP endpoint is `POST /whatsapp`. In live mode, `TWILIO_WEBHOOK_URL` must exactly match the public webhook URL to validate Twilio's signature. The server responds promptly with TwiML, then processes the message and sends the detailed reply through Twilio's REST API. The default live AI provider is Gemini (`GEMINI_MODEL=gemini-3.8-flash`) at `low` thinking level for short fixed tasks; OpenAI remains selectable with `AI_PROVIDER=openai`. Both use structured JSON schemas for triage, specialist, and follow-up outputs; incomplete model results are rejected rather than presented as a diagnosis.

SQLite tables: `cases`, `messages`, `photos`, `tasks`, and `agent_actions`. `MessageSid` is unique for duplicate delivery protection. The `agent_actions` rows are the evidence that triage, dispatcher, and specialist steps actually ran. Keep these records or provide equivalent records if the adapter changes.

The internal `/observer` page reads those same SQLite actions every 1.5 seconds and shows Bob's cartoon avatar from `turnpilot/static/bob.png`. The `/chat` page is a WhatsApp-style browser preview with Bob's avatar, photo upload, four staged sample photos available through the + menu, saved chat history, and Gemini text replies. Scripted, read-only sample conversations fill the sidebar; only Bob is live. A photo produces two short messages: a brief observation and a natural question about finding the right specialist, with a one-time note that the call is simulated. A `yes` triggers dispatcher and specialist; Bob says he is checking, then relays the fictional answer in short bubbles. The plumber draft proposes tomorrow at 17:00 and about two hours of work, with normal plumbing wear marked as an owner cost in the demo. The cleaner draft proposes tomorrow at 10:00, about two hours, and $100/hour. These are **simulated demo slots and prices**, not real availability or quotes. Bob asks whether the time works, then gives a short closing reply after another `yes`. A new photo after a completed draft starts a separate case unless marked `update` or `same issue`; `new issue` also clears earlier case text from the model context. The ↻ button rotates the browser session without deleting stored data. Set `APP_MODE=demo` and `GEMINI_API_KEY` to run real Gemini analysis without Twilio credentials. Direct loopback demo access opens without HTTP Basic because the in-app browser could not complete that sign-in. Forwarded/tunnel access and live mode still require `OBSERVER_PASSWORD` and browser Basic Auth username `demo`; without a password those routes are disabled. The browser preview never sends WhatsApp or contacts a vendor. Bob can close a draft by replying `cancel`; the server marks the case and draft tasks cancelled, logs `bob/cancelled_draft`, and explicitly says no vendor was contacted or booked. Payment questions do not assign liability from a photo.

`assets/bob-whatsapp-profile.png` is the opaque square image for a future approved WhatsApp sender profile. The shared Twilio Sandbox always shows Twilio's logo, so do not promise Bob's face will appear on the test number. The two-founder, no-slides pitch is in `docs/TWO_MINUTE_PITCH.md`; Michel owns the hero story and drives the screen, while Gabi explains the working agents and future property-care agents. The proposed turnover, concierge, listing, pricing, marketing, and portfolio briefing agents are **not implemented**.

## How to test before merging

From the PR branch, follow `README.md` and run `python -m pytest -q` and `python -m turnpilot.demo --db data/demo.db`. The fixture is `demo/broken_sink.png`; Bob's avatar is `turnpilot/static/bob.png`. The mock mode does **not** analyze pixels and does **not** send WhatsApp messages; it tests routing and conversation logic. On 2026-10-26, all four staged inspection images were routed to the expected trade by live Gemini 3.8 Flash triage. The new browser chat was also tested with a real sink photo upload, Gemini plumber reply, and urgency follow-up, with image and messages persisted. The key is kept only in an ignored local `.env`. The app retries when Gemini's thinking tokens exhaust the first output allowance and keeps the simulated-vendor disclosure inside the reply. The Twilio phone test still needs Founder 1's Sandbox credentials and public webhook. Run `python -m turnpilot.verify_model` again after any provider or model change.

Live acceptance sequence after integration: join the Twilio Sandbox; send the staged sink photo from a joined WhatsApp account; receive the acknowledgement and Bob's consent question; reply `yes`; receive the simulated plumber-agent draft; ask about urgency, cost, and next steps in the same chat; confirm SQLite has one case, a photo, a task, message history, and triage, dispatcher, and specialist action rows. Confirm all vendor outreach, estimates, and slots are labeled simulated.

## Merge coordination

1. Gabi's agent should compare her branch with PR #1 and decide on **one** webhook/runtime path.
2. Preserve the distinct agent calls and their persisted action trail while wiring them to the chosen WhatsApp adapter.
3. Run the local tests and one real Sandbox photo conversation before merging. If both branches modify the same files, resolve the conflict by preserving the working provider setup and the agent contract above.
4. The live Gemini photo pass is verified. Neither founder should report a live WhatsApp phone pass until Twilio credentials and the Sandbox photo conversation succeed.

## Candidate next agent

For a host with multiple short-term rentals and Zillow listings, a **property readiness agent** is the strongest next companion to Bob. Bob would pass it the verified damage case; it would draft guest/tenant impact notes, a turnover checklist, and a recommendation to review any affected listing or campaign. It would not message guests, change listings, spend on ads, or contact vendors without an authorized integration. A separate listing/campaign agent can follow once property data and marketing goals are defined. This is a proposal, not implemented behavior.

## Update protocol for future Founder 2 pushes

After every Founder 2 push, add a comment to PR #1 with: commit or branch, changed behavior/files, integration contract changes, tests run and results, and anything Gabi's agent needs to do or avoid. Keep this document current when the contract changes. Use the PR as the shared handoff so both coding agents can read the same state. Do not merge until both founders have completed the live acceptance sequence.
