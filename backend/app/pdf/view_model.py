"""PDF view model builder for the "маршрутный лист" PDF.

The PDF is no longer assembled from raw `state` + LLM enrichments. Instead
``download_pdf`` calls ``build_view_model(state, session_id, kb)`` to produce
a single dict with all content the renderer needs. Static blocks
(preparation checklist, questions to the doctor, urgent-care guidance,
disclaimer) live as module-level constants here; dynamic fields come from
``state`` (triage urgency, routing specialists, intake symptoms,
crisis lock state).

No new LLM calls are made during PDF generation. Routing-time LLM output
(``run_triage``) is consumed via state but not re-validated here — the
``output_safety`` filter already runs at the original call site for the
fields that survive into the view model.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.medical_kb.knowledge_base import MedicalKB
from app.services.output_safety import log_block, validate_output


# Substring matching threshold. Synonyms shorter than this do NOT participate
# in the substring fallback path — they are accepted only via exact-key
# lookup. This protects the route PDF from false-positive matches against
# short KB synonyms like "жар" (3), "37" (2), "зуд" (3): otherwise an
# unrelated input like "пожар на кухне" or "архив 37" could be tagged
# with a medical canonical label. Exact-lookup still accepts short
# synonyms when the input is a clean single-token match.
_SUBSTRING_MIN_LEN = 6


# Map of normalised KB form → KB symptom key. Built lazily, once per KB
# instance, via the cache below. Each KB form is the canonical name OR
# any synonym, run through ``_normalize_for_match``. Sorted (in the
# matching loop, not here) by length descending so more specific forms
# match before less specific ones.
_searchable_forms_cache: dict[int, list[tuple[str, str]]] = {}


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

    The crisis branch in the renderer uses ``is_crisis_only`` and
    ``history_lines``. The non-crisis renderer uses the rest.
    ``primary_route`` is ``None`` if routing produced no specialists; the
    renderer is expected to skip the "next step" card in that case.
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


def _normalize_for_match(text: str) -> str:
    """Canonicalise patient/LLM text for KB lookup.
    Lower-case, ё→е, collapse internal whitespace, trim leading/trailing
    spaces and decorative punctuation. Digits, dots and commas inside the
    string are kept so callers can still recognise forms like
    "температура 37.0" via substring matching."""
    if not text:
        return ""
    s = text.strip().lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" \t\r\n.,;:!?\"'()«»—–-")
    return s


def _searchable_forms(kb: MedicalKB) -> list[tuple[str, str]]:
    """Return a list of (normalised_form, kb_key) covering canonical names
    plus all synonyms. Sorted by form length descending so more specific
    matches win. Built once per kb instance, cached by id(kb)."""
    cached = _searchable_forms_cache.get(id(kb))
    if cached is not None:
        return cached
    forms: list[tuple[str, str]] = []
    for key, data in kb.symptoms.items():
        name = _normalize_for_match(data.get("name", ""))
        if name:
            forms.append((name, key))
        for syn in data.get("synonyms", []) or []:
            n = _normalize_for_match(str(syn))
            if n:
                forms.append((n, key))
    forms.sort(key=lambda t: len(t[0]), reverse=True)
    _searchable_forms_cache[id(kb)] = forms
    return forms


def _normalize_symptoms(state_symptoms: list, kb: MedicalKB) -> list[str]:
    """Deduplicated patient-language symptom list for the "Что вы описали"
    section.

    Resolution order, per symptom:
      1. Exact match of the normalised raw name against the KB synonym
         index (covers clean single-form inputs).
      2. Deterministic substring match against KB canonical+synonym
         forms of length >= ``_SUBSTRING_MIN_LEN``, sorted longest-first.
         This rescues inputs like "температура 37.0" where the synonym
         ("температура") exists but exact lookup misses the suffixed
         form, and inputs like "сильно болит голова" that wrap a known
         synonym in extra patient context.
      3. If no KB match: fall back to the raw name, but only if it passes
         ``validate_output`` — drops LLM-poisoned names like
         "у вас вероятно гастрит".

    Deduplication uses the KB key when matched (so "Головная боль",
    "болит голова" and "боль в голове" collapse to one entry), otherwise
    the normalised raw label."""
    seen_keys: set[str] = set()       # for KB-matched entries
    seen_labels: set[str] = set()     # for raw fallback entries
    out: list[str] = []

    for s in state_symptoms:
        if not isinstance(s, dict):
            continue
        raw = (s.get("name") or "").strip()
        if not raw:
            continue

        normalised = _normalize_for_match(raw)
        if not normalised:
            continue

        # 1. Exact key lookup against the KB synonym index (already
        # lower-cased; ё→е applied here makes the lookup case- and
        # ё-tolerant compared to the bare _synonym_index).
        key = kb._synonym_index.get(normalised)

        # 2. Substring fallback against KB forms >= min-length.
        if not (key and key in kb.symptoms):
            for form, form_key in _searchable_forms(kb):
                if len(form) < _SUBSTRING_MIN_LEN:
                    break  # forms are sorted desc by length; we're done
                if form in normalised:
                    key = form_key
                    break

        if key and key in kb.symptoms:
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(kb.symptoms[key].get("name", "").strip() or raw)
            continue

        # 3. No KB match — accept raw only if it would not breach
        # output_safety (e.g. an LLM that decided to write "цефалгия"
        # or "примите парацетамол" gets dropped here, not the PDF).
        r = validate_output(raw)
        if not r.is_safe:
            continue
        label = raw
        if normalised in seen_labels:
            continue
        seen_labels.add(normalised)
        out.append(label)
    return out


def _filter_history_assistant_lines(history_lines: list) -> list[str]:
    """Return a copy of history with unsafe assistant lines omitted.
    Patient lines are NEVER filtered. Any bracket-prefixed system lines
    are kept as-is — the renderer skips them via its own pattern check."""
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
