"""Output safety validator and helpers.

Filters user-facing LLM-generated text against a curated list of unsafe
formulations (diagnoses, medication names, directive verbs, dosages,
ICD codes). Patterns live in ``medical_kb/output_safety.yaml`` and are
compiled once at startup.

Two entry points for callers:

- ``validate_output(text)`` — pure check, returns a ``ValidationResult``.
  Caller is responsible for logging on block.
- ``safe_generate_text(llm_call, system, ...)`` — async wrapper around
  plain-text LLM calls (clarification, file_analysis). Handles the
  validate → retry-with-reinforced-system → fallback flow and logs each
  outcome. JSON-call sites do their own retry inline because the retry
  must preserve the JSON contract.

A separate helper ``preflight_pdf_data(pdf_data)`` re-validates persisted
LLM-derived content already in ``session.state_json`` before it reaches
``PDFGenerator``. This guards against unsafe content produced before the
filter was introduced, or persisted via paths the per-call filter does
not cover (assistant lines stored in history, ``uploaded_files[].analysis``).

Logging policy (152-ФЗ): we log channel, field name, matched categories,
matched pattern ids, retry/fallback flags. We do NOT log the LLM output
text, the patient input, or any session identifier. Pattern ids and
category names come from our YAML and are safe project config.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result + validator
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    is_safe: bool
    matched_categories: list[str] = field(default_factory=list)
    matched_pattern_ids: list[str] = field(default_factory=list)
    matched_pattern_count: int = 0


@dataclass(frozen=True)
class _CompiledPattern:
    pattern_id: str
    category: str
    compiled: re.Pattern


class OutputSafetyValidator:
    """Loads patterns from YAML once and exposes ``validate(text)``."""

    def __init__(self, yaml_path: Path | None = None):
        if yaml_path is None:
            yaml_path = Path(os.path.dirname(__file__)).parent / "medical_kb" / "output_safety.yaml"
        with open(yaml_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        compiled: list[_CompiledPattern] = []
        for category, data in (raw.get("output_safety") or {}).items():
            match_type = data.get("match_type", "substring")
            for entry in data.get("patterns", []):
                pattern_id = entry["id"]
                pattern = entry["pattern"]
                compiled.append(
                    _CompiledPattern(
                        pattern_id=pattern_id,
                        category=category,
                        compiled=_compile(pattern, match_type),
                    )
                )
        self._patterns = compiled

    def validate(self, text: str) -> ValidationResult:
        if not text:
            return ValidationResult(is_safe=True)
        matched_categories: list[str] = []
        matched_ids: list[str] = []
        seen_categories: set[str] = set()
        for p in self._patterns:
            if p.compiled.search(text):
                matched_ids.append(p.pattern_id)
                if p.category not in seen_categories:
                    seen_categories.add(p.category)
                    matched_categories.append(p.category)
        return ValidationResult(
            is_safe=not matched_ids,
            matched_categories=matched_categories,
            matched_pattern_ids=matched_ids,
            matched_pattern_count=len(matched_ids),
        )


def _compile(pattern: str, match_type: str) -> re.Pattern:
    if match_type == "substring":
        return re.compile(re.escape(pattern), re.IGNORECASE)
    if match_type == "word":
        return re.compile(r"\b" + re.escape(pattern) + r"\b", re.IGNORECASE)
    if match_type == "regex":
        return re.compile(pattern, re.IGNORECASE)
    raise ValueError(f"unknown match_type: {match_type!r}")


_validator: OutputSafetyValidator | None = None


def get_validator() -> OutputSafetyValidator:
    """Lazy singleton. Mockable in tests by setting ``_validator``."""
    global _validator
    if _validator is None:
        _validator = OutputSafetyValidator()
    return _validator


def reset_validator_for_tests() -> None:
    """Test-only — forces the next ``get_validator()`` to reload."""
    global _validator
    _validator = None


def validate_output(text: str) -> ValidationResult:
    """Pure check. Caller decides whether to log."""
    return get_validator().validate(text)


# ---------------------------------------------------------------------------
# Reinforced prompt for retry
# ---------------------------------------------------------------------------


REINFORCED_SAFETY_RULES = """
ВНИМАНИЕ: предыдущий ответ содержал ЗАПРЕЩЁННЫЕ формулировки.
Повтори ответ с СТРОГИМ соблюдением правил.

Запрещено:
— называть пациенту конкретные заболевания;
— называть конкретные лекарства, БАДы, добавки;
— давать императивные указания принять/выпить/сделать что-либо;
— указывать дозировки, частоту или длительность приёма;
— упоминать коды МКБ;
— объяснять возможные причины симптомов («это может быть из-за…», «связано с системой…»).

Если без запрещённых формулировок ответ теряет смысл — ответь нейтрально:
для общего объяснения — «для уточнения ситуации лучше обратиться к врачу»,
для routing-объяснения — «подходящий первый специалист для очной оценки описанных жалоб»,
для clarification — задай вопрос только о фактах: когда началось, как часто, что усиливает или ослабляет.
""".strip()


# ---------------------------------------------------------------------------
# Fallback templates (drafts — refined in commit 5)
# ---------------------------------------------------------------------------


class FALLBACKS:
    """Per-channel/per-field fallback strings applied when retry also fails."""

    chat_clarification = (
        "Расскажите, пожалуйста, подробнее: когда это началось и как именно проявляется?"
    )
    triage_summary = "Пациент описал жалобы, требующие очной оценки врачом."
    routing_explanation = "Описанные жалобы лучше оценить на очном приёме у врача."
    specialist_reason = "Подходящий первый специалист для очной оценки описанных жалоб."
    complaints_simple = "Жалобы записаны со слов пациента."
    # complaints_medical is HIDDEN on failure (empty string → section skipped
    # in _build_symptoms_section). Better no section than a generic
    # placeholder in a "for the doctor" block.
    complaints_medical = ""
    questions_for_doctor: list[str] = [
        "Что может быть следующим шагом в моей ситуации?",
        "Нужно ли наблюдать симптомы и на что обращать внимание?",
        "Нужно ли обратиться к другому специалисту?",
    ]
    what_to_bring: list[str] = [
        "документ, удостоверяющий личность",
        "медицинский полис, если есть",
        "прошлые результаты анализов или обследований, если есть",
        "список лекарств, которые принимаете",
    ]
    # file_analysis fallback is empty — section still renders filename and
    # the always-on disclaimer per _build_documents_section.
    file_analysis = ""


# ---------------------------------------------------------------------------
# Logging helper (no session_id, no LLM output text — 152-ФЗ)
# ---------------------------------------------------------------------------


def log_block(
    *,
    channel: str,
    field_name: str,
    result: ValidationResult,
    retry_attempted: bool,
    fallback_applied: bool,
) -> None:
    logger.warning(
        "output_safety blocked LLM output | occurred_at=%s channel=%s field=%s "
        "matched_categories=%s matched_pattern_ids=%s pattern_hits=%d "
        "retry_attempted=%s fallback_applied=%s",
        datetime.now(timezone.utc).isoformat(),
        channel,
        field_name,
        sorted(result.matched_categories),
        sorted(result.matched_pattern_ids),
        result.matched_pattern_count,
        retry_attempted,
        fallback_applied,
    )


# ---------------------------------------------------------------------------
# Plain-text safe-generate wrapper
# ---------------------------------------------------------------------------


# Async callable that the caller provides; encapsulates everything
# (prompt, images, etc.) except the three knobs the wrapper tweaks for retry.
LLMCallable = Callable[..., Awaitable["_LLMResponseLike"]]


class _LLMResponseLike:
    text: str  # protocol-style: anything with a .text attribute


async def safe_generate_text(
    llm_call: LLMCallable,
    system: str,
    channel: str,
    field_name: str,
    fallback: str,
    base_temperature: float = 0.3,
) -> str:
    """First call → validate → on unsafe: retry once with reinforced system
    and temperature=0.0 → validate again → on second fail: return fallback.

    ``llm_call`` is an async callable of the form::

        async def call(system: str, *, temperature: float, use_cache: bool):
            return await llm.generate(prompt, system, ..., temperature=..., use_cache=...)

    The wrapper does not see prompt / images / cache_ttl — only the three
    knobs above. JSON callers do not use this wrapper because retry must
    preserve the JSON contract; they call ``validate_output`` inline.
    """
    first = await llm_call(system, temperature=base_temperature, use_cache=False)
    result = get_validator().validate(first.text)
    if result.is_safe:
        return first.text

    # Retry once with reinforced safety rules + deterministic temperature.
    log_block(
        channel=channel, field_name=field_name, result=result,
        retry_attempted=True, fallback_applied=False,
    )
    reinforced = system + "\n\n" + REINFORCED_SAFETY_RULES
    second = await llm_call(reinforced, temperature=0.0, use_cache=False)
    result2 = get_validator().validate(second.text)
    if result2.is_safe:
        return second.text

    log_block(
        channel=channel, field_name=field_name, result=result2,
        retry_attempted=True, fallback_applied=True,
    )
    return fallback


# ---------------------------------------------------------------------------
# PDF preflight — re-validate persisted LLM-derived content
# ---------------------------------------------------------------------------


# Fields in pdf_data that are LLM-derived and must be re-validated as
# defence-in-depth. Maps field name → fallback resolver. Per-element fields
# (specialists list, lists of strings) are handled specially below.
_PDF_LLM_FIELDS = {
    "complaints_simple": FALLBACKS.complaints_simple,
    "complaints_medical": FALLBACKS.complaints_medical,
    "routing_explanation": FALLBACKS.routing_explanation,
}


def preflight_pdf_data(pdf_data: dict) -> dict:
    """Return a sanitised copy of ``pdf_data`` ready for ``PDFGenerator``.

    - assistant lines in ``history_lines``: unsafe → omitted entirely
      (no '[удалено]' placeholder). Patient lines are NEVER filtered.
    - ``uploaded_files[].analysis``: unsafe → "". filename/type preserved.
    - LLM-derived top-level fields: unsafe → fallback (or "").
    - ``specialists[].reason``: unsafe → ``FALLBACKS.specialist_reason``.
    - ``specialists[].preparation[]``: unsafe items dropped; if list becomes
      empty, KB fallback is applied by the caller (this function only knows
      about pdf_data, not the KB).
    - ``questions_for_doctor[]``: unsafe items dropped; if empty after
      filtering, replaced wholesale by ``FALLBACKS.questions_for_doctor``.
    - ``what_to_bring[]``: same policy as questions_for_doctor.
    """
    validator = get_validator()
    out = dict(pdf_data)

    out["history_lines"] = _filter_history(pdf_data.get("history_lines", []), validator)
    out["uploaded_files"] = _filter_uploaded_files(pdf_data.get("uploaded_files", []), validator)

    for key, fallback in _PDF_LLM_FIELDS.items():
        val = pdf_data.get(key, "")
        if val:
            r = validator.validate(val)
            if not r.is_safe:
                log_block(
                    channel="pdf_preflight", field_name=key, result=r,
                    retry_attempted=False, fallback_applied=True,
                )
                out[key] = fallback

    out["specialists"] = _filter_specialists(pdf_data.get("specialists", []), validator)
    out["questions_for_doctor"] = _filter_list_or_fallback(
        pdf_data.get("questions_for_doctor", []),
        validator,
        "pdf_preflight", "questions_for_doctor",
        FALLBACKS.questions_for_doctor,
    )
    out["what_to_bring"] = _filter_list_or_fallback(
        pdf_data.get("what_to_bring", []),
        validator,
        "pdf_preflight", "what_to_bring",
        FALLBACKS.what_to_bring,
    )
    return out


def _filter_history(history_lines: list[str], validator: OutputSafetyValidator) -> list[str]:
    kept: list[str] = []
    for line in history_lines:
        if not isinstance(line, str):
            kept.append(line)
            continue
        # Only filter assistant lines. Patient lines are never touched —
        # they are the patient's own words.
        if line.startswith("Ассистент: "):
            body = line[len("Ассистент: "):]
            r = validator.validate(body)
            if not r.is_safe:
                log_block(
                    channel="pdf_preflight", field_name="history_line",
                    result=r, retry_attempted=False, fallback_applied=True,
                )
                continue  # omit, no placeholder
        kept.append(line)
    return kept


def _filter_uploaded_files(files: list[dict], validator: OutputSafetyValidator) -> list[dict]:
    out: list[dict] = []
    for f in files:
        if not isinstance(f, dict):
            out.append(f)
            continue
        analysis = (f.get("analysis") or "").strip()
        if analysis:
            r = validator.validate(analysis)
            if not r.is_safe:
                log_block(
                    channel="pdf_preflight", field_name="uploaded_file_analysis",
                    result=r, retry_attempted=False, fallback_applied=True,
                )
                f = {**f, "analysis": ""}
        out.append(f)
    return out


def _filter_specialists(
    specialists: list[dict],
    validator: OutputSafetyValidator,
) -> list[dict]:
    out: list[dict] = []
    for spec in specialists:
        if not isinstance(spec, dict):
            out.append(spec)
            continue
        new = dict(spec)
        reason = new.get("reason", "")
        if reason:
            r = validator.validate(reason)
            if not r.is_safe:
                log_block(
                    channel="pdf_preflight", field_name="specialist_reason",
                    result=r, retry_attempted=False, fallback_applied=True,
                )
                new["reason"] = FALLBACKS.specialist_reason
        prep = new.get("preparation", []) or []
        if prep:
            filtered_prep: list[str] = []
            for item in prep:
                if not isinstance(item, str):
                    continue
                r = validator.validate(item)
                if r.is_safe:
                    filtered_prep.append(item)
                else:
                    log_block(
                        channel="pdf_preflight", field_name="specialist_preparation_item",
                        result=r, retry_attempted=False, fallback_applied=True,
                    )
            new["preparation"] = filtered_prep
        out.append(new)
    return out


def _filter_list_or_fallback(
    items: list,
    validator: OutputSafetyValidator,
    channel: str,
    field_name: str,
    fallback_list: list[str],
) -> list[str]:
    if not items:
        return items
    kept: list[str] = []
    any_blocked = False
    for item in items:
        if not isinstance(item, str):
            continue
        r = validator.validate(item)
        if r.is_safe:
            kept.append(item)
        else:
            any_blocked = True
            log_block(
                channel=channel, field_name=field_name, result=r,
                retry_attempted=False, fallback_applied=True,
            )
    if not kept and any_blocked:
        return list(fallback_list)
    return kept
