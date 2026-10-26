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
    route = dispatch(triage_result)
    if on_action:
        on_action("dispatcher", "routed", route)
    plan = specialist(settings, triage_result, property_context)
    if on_action:
        on_action(route["route"], "draft_plan", plan)
    intro = "[MOCK MODE] " if settings.mode == "mock" else ""
    caution = (
        " If there is exposed wiring, fire risk, or water near electricity, keep clear and seek urgent qualified help."
        if triage_result["severity"] == "high"
        else ""
    )
    reply = (
        f"{intro}I can see/report: {triage_result['issue']}. {triage_result['uncertainty']}\n"
        f"Connected to simulated {triage_result['trade'].title()} Agent. Draft repair request prepared.\n"
        f"Possible diagnosis: {plan['diagnosis']}\nPlan: {plan['repair_plan']}\n"
        f"Rough demo-only estimate: {plan['estimate']}. Simulated slot: {plan['slot']}.\n"
        f"No vendor has been contacted or booked.{caution} Ask me a follow-up question."
    )
    return InspectionResult(triage_result, route, plan, reply[:1550])
