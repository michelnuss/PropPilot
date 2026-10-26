# Founder handoff: TurnPilot AI hackathon MVP

This document is for both founders and their coding agents. Read it with [PR #1](https://github.com/gabriellaec/PropPilot/pull/1) before integrating. The repository's `main` branch had only `# PropPilot` when Founder 2 began; this PR is a working implementation on a fork, not a change already merged into `main`.

## Ownership

| Founder | Primary responsibility | Current status |
| --- | --- | --- |
| Gabi, Founder 1 | WhatsApp Sandbox, webhook, media transport, public tunnel, credentials, live phone test, deployment/runtime | Her implementation is not yet visible on `main`; coordinate before replacing an existing adapter. |
| Michel, Founder 2 | Vision triage, dispatcher, trade specialist agents, follow-up reasoning, demo scenario, agent action evidence | Implemented in this PR, with a provisional Twilio adapter and SQLite store so the whole flow can be tested. |

The provisional Twilio adapter in `turnpilot/server.py` exists to make Founder 2's workflow runnable end to end. Gabi can reuse it or connect her own adapter to the agent functions; please avoid building two competing inbound webhook flows in the merged app.

## Integration contract for Gabi's coding agent

The preferred integration entry point is `turnpilot/workflow.py`:

- `inspect(settings, message, image, prior_case=None, property_context="", on_action=None) -> InspectionResult` runs triage → dispatcher → specialist without importing WhatsApp or SQLite. `InspectionResult` contains `triage`, `dispatch`, `specialist`, and a ready-to-send `reply`. Pass `on_action(agent, action, detail)` to persist each step as it completes; this preserves triage and dispatch logs even if the specialist fails.

The lower-level agent functions remain available in `turnpilot/agents.py`:

- `triage(settings, message, image, prior) -> dict`: `image` is `(Path, MIME type)` after the server has downloaded the photo. It returns `trade`, `issue`, `severity`, `visible_evidence`, and `uncertainty`. In live mode it sends actual image bytes to a vision-capable model; in mock mode it follows the staged sink fixture and labels the result as mock.
- `dispatch(triage_result) -> dict`: chooses the specialist route and marks it simulated.
- `specialist(settings, triage_result, property_context) -> dict`: produces `diagnosis`, `repair_plan`, `estimate`, `slot`, and `simulated=True`. It does not contact anyone.
- `follow_up(settings, question, case, history) -> str`: answers using the stored case and recent messages. The case has `plan_json`, `issue`, `trade`, `severity`, and `property_context`.

`turnpilot/server.py` currently calls `inspect(...)`, uses the callback to log each handoff in SQLite via `turnpilot/db.py`, and sends the final response through Twilio. The HTTP endpoint is `POST /whatsapp`. In live mode, `TWILIO_WEBHOOK_URL` must exactly match the public webhook URL to validate Twilio's signature. The server responds promptly with TwiML, then processes the message and sends the detailed reply through Twilio's REST API. The default live AI provider is Gemini (`GEMINI_MODEL=gemini-3.8-flash`); OpenAI remains selectable with `AI_PROVIDER=openai`. Both use structured JSON schemas for triage, specialist, and follow-up outputs; incomplete model results are rejected rather than presented as a diagnosis.

SQLite tables: `cases`, `messages`, `photos`, `tasks`, and `agent_actions`. `MessageSid` is unique for duplicate delivery protection. The `agent_actions` rows are the evidence that triage, dispatcher, and specialist steps actually ran. Keep these records or provide equivalent records if the adapter changes.

## How to test before merging

From the PR branch, follow `README.md` and run `python -m pytest -q` and `python -m turnpilot.demo --db data/demo.db`. The fixture is `demo/broken_sink.png`; `demo/turnpilot-avatar.png` is optional agent artwork. The mock mode does **not** analyze pixels and does **not** send WhatsApp messages; it tests routing and conversation logic. As of the latest Founder 2 push, fifteen tests pass, and a localhost HTTP photo plus cost follow-up worked. A Gemini credential tested on 2026-10-26 was rejected with HTTP 401, and no live model or Twilio phone test has passed. With a valid key set locally, run `python -m turnpilot.verify_model` before the joint WhatsApp test.

Live acceptance sequence after integration: join the Twilio Sandbox; send the staged sink photo from a joined WhatsApp account; receive the acknowledgement and plumber-agent draft; ask about urgency, cost, and next steps in the same chat; confirm SQLite has one case, a photo, a task, message history, and three agent action rows. Confirm all vendor outreach, estimates, and slots are labeled simulated.

## Merge coordination

1. Gabi's agent should compare her branch with PR #1 and decide on **one** webhook/runtime path.
2. Preserve the distinct agent calls and their persisted action trail while wiring them to the chosen WhatsApp adapter.
3. Run the local tests and one real Sandbox photo conversation before merging. If both branches modify the same files, resolve the conflict by preserving the working provider setup and the agent contract above.
4. Neither founder should report a live WhatsApp or vision-model pass until credentials are configured and the phone test succeeds.

## Update protocol for future Founder 2 pushes

After every Founder 2 push, add a comment to PR #1 with: commit or branch, changed behavior/files, integration contract changes, tests run and results, and anything Gabi's agent needs to do or avoid. Keep this document current when the contract changes. Use the PR as the shared handoff so both coding agents can read the same state. Do not merge until both founders have completed the live acceptance sequence.
