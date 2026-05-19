"""PDF view model builder for the "маршрутный лист" PDF.

The PDF is no longer assembled from raw `state` + LLM enrichments. Instead
``download_pdf`` calls ``build_view_model(state, session_id, kb)`` to produce
a single dict with all content the renderer needs. Static blocks
(preparation checklist, questions to the doctor, urgent-care guidance,
disclaimer) live as module-level constants here; dynamic fields come from
``state`` (triage urgency, routing specialists, intake symptoms,
uploaded file names, crisis lock state).

No new LLM calls are made during PDF generation. Routing-time LLM output
(``run_triage``) is consumed via state but not re-validated here — the
``output_safety`` filter already runs at the original call site for the
fields that survive into the view model.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.medical_kb.knowledge_base import MedicalKB
from app.services.output_safety import log_block, validate_output


# ---------------------------------------------------------------------------
# Static content blocks (per docs/product/pdf-redesign-brief.md §3)
# ---------------------------------------------------------------------------


URGENCY_TIMEFRAME_MAP = {
    "low": "Плановый визит, по возможности",
    "medium": "Записаться в ближайшие 1–2 дня",
    "high": "Обратиться к врачу сегодня",
    "emergency": "Обратиться за срочной медицинской помощью",
}


# Brief §3.2 — neutral safe one-liner under the primary specialist.
# Static; no causal explanation, no "может быть связано" framing.
PRIMARY_ROUTE_ONE_LINER = (
    "Это подходящий первый специалист для очной оценки описанных жалоб "
    "и решения, нужен ли другой врач."
)


# Brief §3.5 — one shared checklist (max 5), not per-specialty.
PREPARATION_CHECKLIST: list[str] = [
    "вспомните, когда начались жалобы",
    "отметьте, что усиливает или уменьшает симптомы",
    "возьмите список лекарств, витаминов и добавок",
    "возьмите прошлые анализы, снимки или выписки, если они есть",
]


# Brief §3.6 — generic, 2-3 max. No medical agenda, no directed differential.
QUESTIONS_FOR_DOCTOR: list[str] = [
    "Что может быть следующим шагом в моей ситуации?",
    "Нужно ли наблюдать, как меняется состояние?",
    "Нужно ли мне обратиться к другому специалисту?",
]


# Brief §3.7 — static guidance, NOT LLM-generated, no symptom-specific
# red-flag list. The triage/red_flags machinery still runs at intake time
# and may trigger the crisis-banner branch; in the routine PDF we just give
# generic urgency guidance.
URGENT_CARE_BLOCK = (
    "Если состояние резко ухудшается, симптомы быстро нарастают, "
    "появляется ощущение угрозы жизни или вы не можете дождаться "
    "планового приёма — обратитесь за срочной медицинской помощью. "
    "В экстренной ситуации звоните 103 или 112."
)


# Brief §3.8 — compact two-line disclaimer.
DISCLAIMER = (
    "MedNavigator не ставит диагноз и не назначает лечение. "
    "Документ носит справочный характер и помогает подготовиться к обращению к врачу."
)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_view_model(state: dict, session_id: str, kb: MedicalKB) -> dict:
    """Assemble the view model used by ``PDFGenerator.generate(view_model, ...)``.

    Pure function over ``state``. No I/O, no LLM, no network.

    ``state`` is the deserialised ``Session.state_json``. Expected shape
    follows what ``send_message`` / ``run_triage`` produce — but every read
    uses ``.get(...)`` so older or partially-populated states do not crash.

    The crisis branch in the renderer uses ``is_crisis_only``,
    ``history_lines``, and ``uploaded_files``. The non-crisis renderer uses
    the rest. ``primary_route`` is ``None`` if routing produced no
    specialists; the renderer is expected to skip the "next step" card in
    that case.
    """
    triage = state.get("triage") or {}
    routing = state.get("routing") or {}
    specialists = routing.get("specialists") or []
    is_crisis_only = bool(state.get("crisis_locked", False))

    urgency_level = triage.get("urgency", "medium")
    urgency_label = URGENCY_TIMEFRAME_MAP.get(
        urgency_level, URGENCY_TIMEFRAME_MAP["medium"],
    )

    primary_route, secondary_routes = _split_specialists(specialists)
    what_patient_described = _normalize_symptoms(state.get("symptoms") or [], kb)
    uploaded_files = _slim_uploaded_files(state.get("uploaded_files") or [])

    raw_history = state.get("history_lines") or []
    # For the crisis branch the renderer iterates history; defence-in-depth
    # filter from PR #13 stays here. Patient lines are NEVER touched —
    # they are the patient's own words and may legitimately contain medical
    # terms recounted from prior care.
    filtered_history = _filter_history_assistant_lines(raw_history)

    # red_flags is consumed only by the crisis renderer (PR #11 retained
    # behaviour: crisis PDF shows the hotline message at the bottom in
    # addition to the top banner). Computed cheaply (substring matches)
    # only when actually needed.
    red_flags = _collect_red_flags(raw_history, kb) if is_crisis_only else []

    return {
        "session_id_short": session_id[:8],
        "generated_at": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
        "is_crisis_only": is_crisis_only,
        "urgency": {
            "level": urgency_level,
            "label": urgency_label,
        },
        "primary_route": primary_route,
        "secondary_routes": secondary_routes,
        "what_patient_described": what_patient_described,
        "preparation_checklist": list(PREPARATION_CHECKLIST),
        "questions_for_doctor": list(QUESTIONS_FOR_DOCTOR),
        "urgent_care_block": URGENT_CARE_BLOCK,
        "disclaimer": DISCLAIMER,
        "uploaded_files": uploaded_files,
        # Both consumed only by the crisis branch of the renderer.
        "history_lines": filtered_history,
        "red_flags": red_flags,
    }


def _split_specialists(specialists: list) -> tuple[dict | None, list[dict]]:
    """Split routing.specialists into one primary + up to two secondary.
    Each output dict carries only the safe fields the PDF will render —
    raw LLM-generated `reason` is dropped entirely (the renderer uses
    PRIMARY_ROUTE_ONE_LINER instead)."""
    cleaned: list[dict] = []
    for s in specialists:
        if not isinstance(s, dict):
            continue
        name = (s.get("specialty") or "").strip()
        if not name:
            continue
        cleaned.append({"specialty": name})
        if len(cleaned) >= 3:
            break
    if not cleaned:
        return None, []
    primary = {
        "specialty": cleaned[0]["specialty"],
        "safe_one_liner": PRIMARY_ROUTE_ONE_LINER,
    }
    secondary = cleaned[1:3]
    return primary, secondary


def _normalize_symptoms(state_symptoms: list, kb: MedicalKB) -> list[str]:
    """Deduplicated patient-language symptom list for the "Что вы описали"
    section. Resolves each LLM-extracted symptom name through the KB
    synonym index to the canonical patient-friendly name from
    ``symptoms.yaml``. Symptoms outside the KB are accepted only if they
    pass ``validate_output`` — patient-language phrasing typically does."""
    seen: set[str] = set()
    out: list[str] = []
    for s in state_symptoms:
        if not isinstance(s, dict):
            continue
        raw = (s.get("name") or "").strip()
        if not raw:
            continue
        key = kb._synonym_index.get(raw.lower())
        if key and key in kb.symptoms:
            label = kb.symptoms[key].get("name", "").strip() or raw
        else:
            # Fallback: raw extracted name, only if it would not breach
            # output_safety (e.g. an LLM that decided to write "цефалгия"
            # gets dropped here rather than reaching the PDF).
            r = validate_output(raw)
            if not r.is_safe:
                continue
            label = raw
        key_norm = label.lower()
        if key_norm in seen:
            continue
        seen.add(key_norm)
        out.append(label)
    return out


def _filter_history_assistant_lines(history_lines: list) -> list[str]:
    """Return a copy of history with unsafe assistant lines omitted.
    Patient lines are NEVER filtered. Bracket-prefixed system lines
    (e.g. "[Анализ документа: ...]") are kept as-is — the renderer
    skips them via its own pattern check."""
    out: list[str] = []
    for line in history_lines:
        if not isinstance(line, str):
            continue
        if line.startswith("Ассистент: "):
            body = line[len("Ассистент: "):]
            r = validate_output(body)
            if not r.is_safe:
                log_block(
                    channel="pdf_view_model", field_name="history_line",
                    result=r, retry_attempted=False, fallback_applied=True,
                )
                continue
        out.append(line)
    return out


def _collect_red_flags(history_lines: list, kb: MedicalKB) -> list[str]:
    """Re-derive red-flag messages from patient turns. Used by the crisis
    renderer; cheap (substring matches against red_flags.yaml)."""
    out: list[str] = []
    seen: set[str] = set()
    for line in history_lines:
        if not isinstance(line, str) or not line.startswith("Пациент: "):
            continue
        rf_text = line[len("Пациент: "):]
        flag = kb.check_red_flags(rf_text)
        if flag and isinstance(flag, dict):
            msg = flag.get("message", "")
            if msg and msg not in seen:
                seen.add(msg)
                out.append(msg)
    return out


def _slim_uploaded_files(files: list) -> list[dict]:
    """Strip everything except filename. The PDF no longer renders the
    LLM-derived analysis (only the upload disclaimer + filename), so we
    explicitly do not propagate it into the view model."""
    out: list[dict] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        filename = (f.get("filename") or "").strip()
        if not filename:
            continue
        out.append({"filename": filename})
    return out
