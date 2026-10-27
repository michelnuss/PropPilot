# Staged inspection photos for Bob

These are AI-generated demo scenes, not real property incidents. Send **one image at a time** in the joined Twilio WhatsApp Sandbox conversation. Add `new issue` to the caption when you want a separate case rather than an update to the active case.

| File | Visible issue | Expected route |
| --- | --- | --- |
| `leaking-sink.png` | Broken under-sink drain with active water leak | Plumber agent |
| `damaged-outlet.png` | Cracked, scorched outlet with exposed wiring | Electrician agent; urgent caution |
| `broken-window.png` | Cracked and partially missing window glass | General maintenance agent; safety caution |
| `turnover-cleaning.png` | Dirty bathroom floor, towels, and overflowing bin | Cleaner agent |

All four images were sent directly to the live Gemini 3.8 Flash triage function on 2026-10-26 and routed to the expected trade. That is **not** a WhatsApp phone test; Twilio Sandbox credentials and webhook setup are still required. Estimates, availability, and specialist handoffs remain simulated.

Use `APP_MODE=live` with a valid Gemini key for visual routing across all four scenes. The deterministic `APP_MODE=mock` fixture routes every image as a sink/plumber case regardless of its pixels.
