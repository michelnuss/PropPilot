"""Founder 2's agent workflow; independent of WhatsApp and SQLite."""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from .agents import dispatch, specialist, triage
from .config import Settings


@dataclass(frozen=True)
class InspectionResult:
    triage: dict
    dispatch: dict
    specialist: dict
    reply: str


def assess_photo(settings: Settings, message: str, image: tuple[Path, str],
                 prior_case: dict | None = None,
                 on_action: Callable[[str, str, dict], None] | None = None) -> dict:
    """Inspect a photo before any simulated specialist handoff."""
    result = triage(settings, message, image, prior_case)
    if on_action:
        on_action("inspection_triage", "classified", result)
    return result


def prepare_handoff(settings: Settings, triage_result: dict, property_context: str = "",
                    on_action: Callable[[str, str, dict], None] | None = None) -> tuple[dict, dict]:
    """Run dispatch and specialist only after the person agrees."""
    route = dispatch(triage_result)
    if on_action:
        on_action("dispatcher", "routed", route)
    plan = specialist(settings, triage_result, property_context)
    if on_action:
        on_action(route["route"], "draft_plan", plan)
    return route, plan


def inspect(
    settings: Settings,
    message: str,
    image: tuple[Path, str] | None,
    prior_case: dict | None = None,
    property_context: str = "",
    on_action: Callable[[str, str, dict], None] | None = None,
) -> InspectionResult:
    """Run three agents; optionally report each completed step to the caller."""
    triage_result = triage(settings, message, image, prior_case)
    if on_action:
        on_action("inspection_triage", "classified", triage_result)
    route, plan = prepare_handoff(settings, triage_result, property_context, on_action)
    intro = "[MOCK MODE] " if settings.mode == "mock" else ""
    caution = (" If water is near electricity, there is exposed wiring, or the area is unsafe, keep clear and seek urgent qualified help."
               if triage_result["severity"] == "high" else "")
    reply = (
        f"{intro}I'm back. I checked with our simulated {triage_result['trade'].title()} Agent about what looks like {triage_result['issue'][:160].lower()}. "
        f"{triage_result['uncertainty'][:120]}\n\n"
        f"The draft plan: {plan['repair_plan'][:220]} "
        f"A simulated visit could be {plan['slot'][:80].lower()}. Rough demo-only estimate: {plan['estimate'][:90]}.\n\n"
        f"No real contractor has been contacted or booked. Nothing is confirmed and no payment is requested.{caution} "
        f"Ask me anything, or say 'cancel' to close the draft."
    )
    return InspectionResult(triage_result, route, plan, reply)
