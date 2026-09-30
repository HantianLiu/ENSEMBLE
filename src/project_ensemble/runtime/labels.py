"""Human-facing labels for runtime identities."""

from __future__ import annotations


PERSONA_DISPLAY_LABELS: dict[str, str] = {
    "systems_integrator": "Builder / 建构者",
    "pragmatic_minimalist": "Monitor / 监管者",
    "exploratory_synthesist": "Cartographer / 制图者",
    "librarian": "Librarian / 智库长",
    "science_bookkeeper": "Science Bookkeeper / 科学事实核校员",
    "citation_bookkeeper": "Citation Bookkeeper / 引文核校员",
}


def persona_display_label(persona: str) -> str:
    """Return the bilingual UI label without changing persisted persona codes."""

    return PERSONA_DISPLAY_LABELS.get(persona, persona)
