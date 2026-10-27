# TurnPilot AI — two-minute live pitch

No slides. Screen-share the WhatsApp conversation and TurnPilot's `/observer` page. Rehearse to about two minutes. **Gabi** drives the screen; **Michel** narrates Bob's handoffs.

## Script

**Gabi — 0:00–0:30 (show the WhatsApp photo, before sending)**

Meet Ana. She manages 100 rental homes in Miami. A guest sends this photo: a broken sink pipe. In the old game, Ana identifies the unit, figures out the issue, reaches a plumber, updates the guest, and tracks what happened. At just one issue per home per month and 15 minutes of triage, that's **25 hours monthly** before repairs start. Airbnb expects a response to time-sensitive in-stay issues within an hour during local daytime hours.

**Michel — 0:30–1:20 (send the photo; switch to `/observer`; then return to WhatsApp)**

Now watch the new game. The guest sends the photo in WhatsApp. Bob identifies the visible leak, flags urgency, and routes it to a simulated plumber agent. On this screen, every handoff appears live: Bob, dispatcher, plumber. Ask “How urgent is it?” Bob answers from the photo and case history. Ask “Who pays?” He won't invent a charge. Say “cancel,” and the draft closes. No vendor is contacted or booked in this demo.

**Gabi — 1:20–1:45 (show the case and action log)**

Miami city has about **135,000 renter households**. AirDNA tracks **18,289 active short-term rental listings** across Airbnb, Vrbo, and Booking.com in its Miami market. Ana already has a trusted plumber and cleaner. Our next step is connecting Bob to her team, not selling her another marketplace.

**Michel — 1:45–2:00 (end on Bob's response)**

Ana is the hero. One WhatsApp conversation gives her context, speed, and control across 100 homes. TurnPilot lets her run the portfolio instead of chasing the next leak.

## Number notes for founders and judges

- **~135,000 renter households** is an approximation, not a count of individual people. [U.S. Census QuickFacts for Miami city](https://www.census.gov/quickfacts/fact/table/miamicityflorida/PST040223) reports 194,821 households and a 30.8% owner-occupied rate for 2020–2024. Calculation: 194,821 × 69.2% ≈ 134,816 renter-occupied households.
- **18,289 active short-term rental listings** is [AirDNA's Miami market figure](https://www.airdna.co/vacation-rental-data/app/us/florida/miami/overview), covering September 2026 and updated October 20, 2026. It covers Airbnb, Vrbo, and Booking.com, de-duplicates the same home across channels, and is **not** a count of unique hosts or Airbnb-only properties. Its market boundary need not match the Census city boundary.
- **One-hour response expectation:** [Airbnb's host ground rules](https://www.airbnb.com/help/article/2895) state that for time-sensitive in-stay issues during local daytime hours, hosts are expected to respond within one hour.
- **25 hours/month** is an illustrative scenario: 100 homes × one issue per home per month × 15 minutes of manual triage = 1,500 minutes. It is not measured TurnPilot savings.
- Present the plumber handoff, estimate, and slot as **simulated**. The current repo has verified Gemini photo triage and local webhook tests; a real Twilio phone-to-phone run still requires Sandbox setup and testing.
