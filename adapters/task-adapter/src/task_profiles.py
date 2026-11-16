"""Fixed, opt-in task workflow reminders, independent of tool availability.

Profiles do not execute tools, infer a task, grant permissions, or change the
user's objective. Unmapped sessions use generic; unknown values are rejected.
"""
from __future__ import annotations

_PROFILE_REMINDERS = {
    "generic": (
        "Follow the user's current objective and applicable instructions. "
        "Use the supplied evidence, make scoped changes when requested, and distinguish verified results from uncertainty. "
        "This profile is workflow guidance only; it does not change the task, tool catalog, or permissions."
    ),
    "software_development": (
        "For an authorized software task, inspect the relevant source and existing behavior, make focused changes, "
        "and run meaningful available checks. Report actual test results, failures, and remaining limitations. "
        "A source edit is not proof of deployment or successful execution. This profile does not authorize installation, "
        "deployment, publication, unrelated changes, or any tool absent from the current catalog."
    ),
    "study_notes": (
        "For the user's learning-note task, check the established progress and source material before continuing. "
        "Preserve source notation, page references, and stated assumptions; distinguish independently derived explanations "
        "from source claims. Verify formulas and examples with available evidence and identify unresolved ambiguity. "
        "Do not repeat completed work or publish notes unless the user's task calls for it. "
        "This profile does not change the task, tool catalog, or permissions."
    ),
    "document_creation": (
        "For an authorized document task, follow the requested format and destination, inspect relevant references, "
        "and preserve the established structure. Verify content and, when an applicable declared capability is available, "
        "render and inspect the output. If a check cannot be performed, state that limitation. "
        "This profile does not authorize sending, publishing, extra destinations, or tools absent from the current catalog."
    ),
}
PROFILE_NAMES = frozenset(_PROFILE_REMINDERS)


def profile_reminder(name: str) -> str:
    """Return only a fixed reminder; callers reject unknown configuration."""
    if not isinstance(name, str) or name not in PROFILE_NAMES:
        raise ValueError("unknown_task_profile")
    return _PROFILE_REMINDERS[name]
