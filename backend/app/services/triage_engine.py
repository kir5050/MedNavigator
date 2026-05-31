import json
import logging
import re

from app.llm.manager import LLMManager
from app.medical_kb import MedicalKB
from app.prompts import PromptTemplates
from app.services.output_safety import (
    FALLBACKS,
    REINFORCED_SAFETY_RULES,
    ValidationResult,
    log_block,
    safe_generate_text,
    validate_output,
)

logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict:
    """Extract JSON from LLM response, stripping markdown code blocks if present."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try to find first { ... } block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError("No valid JSON found", text, 0)


class TriageEngine:
    MAX_CLARIFICATIONS = 4

    def __init__(self, llm: LLMManager, kb: MedicalKB):
        self.llm = llm
        self.kb = kb

    _NONSENSE_RE = re.compile(r"[а-яёa-z]", re.IGNORECASE)

    @classmethod
    def _should_reject_as_nonsense(cls, text: str, session_state: dict) -> bool:
        """Decide whether to drop an input as obviously non-text BEFORE the LLM.

        The old guard counted only letters and was state-blind: a numeric
        reply ("33") to a clarification question ("Сколько вам лет?") was
        rejected the same way as "33" sent as a first message. The new
        rule is context-aware:

        - If the message already carries enough letters (>= 2), it always
          passes — same as before, "да"/"нет"/"болит" etc.
        - Pure-whitespace or empty strings are rejected unconditionally.
        - Otherwise we look at session_state for signs that we are inside
          an ongoing clarification dialogue: clarification_count > 0, or
          some symptoms were already collected, or the history carries
          at least one "Ассистент:" turn. If yes, we accept the input
          provided it carries at least one alphanumeric character (so
          "33", "37.0", "5/10" pass; "!!!", "...", emoji-only do not).
        - With no such context, behaviour is unchanged: reject anything
          short of two letters.

        Crisis / red-flag check already runs upstream and never goes
        through here — this helper is concerned only with the nonsense
        fallback path.
        """
        if not text or not text.strip():
            return True
        if len(cls._NONSENSE_RE.findall(text)) >= 2:
            return False
        in_clarification = (
            session_state.get("clarification_count", 0) > 0
            or bool(session_state.get("symptoms"))
            or "Ассистент:" in (session_state.get("history") or "")
        )
        if not in_clarification:
            # First message / no context — preserve original behaviour.
            return True
        # In-context short reply is acceptable iff it carries at least
        # one alphanumeric token. Symbols-only ("!!!", "...") still drop.
        return not any(ch.isalnum() for ch in text)

    async def process_message(
        self, text: str, session_state: dict
    ) -> dict:
        # 1. Red flag check BEFORE anything else
        emergency = self.kb.check_red_flags(text)
        if emergency:
            return {
                "response": emergency["message"],
                "status": "emergency",
                "is_emergency": True,
                "is_crisis": emergency["is_crisis"],
            }

        # 1.5. Reject obviously non-text input — context-aware (see helper).
        if self._should_reject_as_nonsense(text, session_state):
            return {
                "response": "Пожалуйста, опишите словами, что вас беспокоит — "
                            "например, «болит голова» или «температура и кашель».",
                "status": "collecting",
                "is_emergency": False,
                "symptoms": session_state.get("symptoms", []),
                "kb_matches": [],
                "clarification_count": session_state.get("clarification_count", 0),
            }

        history = session_state.get("history", "")
        all_symptoms = session_state.get("symptoms", [])
        clarification_count = session_state.get("clarification_count", 0)

        # 2. Extract symptoms
        system, prompt = PromptTemplates.symptom_extraction(text, history)
        extraction = await self.llm.generate(prompt, system)
        try:
            extracted = extract_json(extraction.text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse symptom extraction: %s", extraction.text)
            extracted = {"symptoms": [], "needs_clarification": True}

        new_symptoms = extracted.get("symptoms", [])
        all_symptoms.extend(new_symptoms)

        # Also match against KB
        kb_matches = self.kb.match_symptoms(text)

        # 3. Decide: clarify or triage
        needs_clarification = extracted.get("needs_clarification", False)
        has_enough_symptoms = len(all_symptoms) >= 2
        min_messages_reached = clarification_count >= 1

        # Always clarify if: not enough symptoms, or LLM says so, or not enough conversation
        should_clarify = (
            (not has_enough_symptoms)
            or needs_clarification
            or (not min_messages_reached)
        )

        if should_clarify and clarification_count < self.MAX_CLARIFICATIONS:
            system, prompt = PromptTemplates.clarification(
                json.dumps(all_symptoms, ensure_ascii=False), history
            )

            llm = self.llm

            async def _call(sys_arg, *, temperature, use_cache):
                return await llm.generate(
                    prompt, sys_arg, temperature=temperature, use_cache=use_cache,
                )

            clarification_text = await safe_generate_text(
                _call, system,
                channel="chat", field_name="clarification",
                fallback=FALLBACKS.chat_clarification,
            )
            return {
                "response": clarification_text,
                "status": "collecting",
                "is_emergency": False,
                "symptoms": all_symptoms,
                "kb_matches": kb_matches,
                "clarification_count": clarification_count + 1,
            }

        # 4. Verify we actually have symptoms before declaring "ready"
        has_real_symptoms = any(
            isinstance(s, dict) and s.get("name") for s in all_symptoms
        )
        has_kb_matches = len(kb_matches) > 0

        if not has_real_symptoms and not has_kb_matches:
            logger.warning(
                "No symptoms after %d clarifications, resetting. symptoms=%s",
                clarification_count, all_symptoms,
            )
            return {
                "response": "К сожалению, я не смог определить симптомы из вашего описания. "
                            "Пожалуйста, опишите словами, что именно вас беспокоит — "
                            "например, «болит голова и тошнит» или «кашель и температура».",
                "status": "collecting",
                "is_emergency": False,
                "symptoms": [],
                "kb_matches": [],
                "clarification_count": max(clarification_count - 2, 0),
            }

        return {
            "response": "Достаточно информации. Вы можете получить рекомендацию или продолжить описывать симптомы.",
            "status": "ready",
            "is_emergency": False,
            "symptoms": all_symptoms,
            "kb_matches": kb_matches,
            "clarification_count": clarification_count,
        }

    async def run_triage(self, session_state: dict) -> dict:
        """Run triage + routing. Called when user explicitly requests result."""
        all_symptoms = session_state.get("symptoms", [])
        history = session_state.get("history", "")

        # Guard: refuse to triage without real symptoms
        has_real = any(
            (isinstance(s, dict) and s.get("name")) or (isinstance(s, str) and len(s) > 1)
            for s in all_symptoms
        )
        if not has_real:
            logger.warning("run_triage called with no real symptoms: %s", all_symptoms)
            return {
                "triage": {
                    "urgency": "low",
                    "urgency_reason": "Симптомы не определены",
                    "medical_areas": [],
                    "summary": "Не удалось определить симптомы. Пожалуйста, опишите жалобы подробнее.",
                },
                "routing": {
                    "specialists": [],
                    "explanation": "Недостаточно данных для рекомендации специалиста.",
                },
                "specialists": [],
            }

        # KB matches from all collected symptoms
        kb_matches = []
        for s in all_symptoms:
            name = s.get("name", "") if isinstance(s, dict) else str(s)
            kb_matches.extend(self.kb.match_symptoms(name))

        # Triage
        symptoms_json = json.dumps(all_symptoms, ensure_ascii=False)
        system, prompt = PromptTemplates.triage(symptoms_json, history)
        triage_result = await self.llm.generate(prompt, system, use_cache=False)
        try:
            triage = extract_json(triage_result.text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse triage: %s", triage_result.text)
            triage = {"urgency": "medium", "medical_areas": [], "summary": ""}

        # Output safety: only `summary` is user-facing in this JSON.
        # If retry fails or returns invalid JSON, we MUST NOT replace the
        # original `triage` dict with a default — that would silently lose
        # the urgency value (e.g. "high" → "medium") and turn a quality
        # issue into a safety regression. Preserve the original parsed
        # structure and only swap the unsafe field.
        summary_check = validate_output(triage.get("summary", ""))
        if not summary_check.is_safe:
            log_block(
                channel="triage", field_name="summary", result=summary_check,
                retry_attempted=True, fallback_applied=False,
            )
            try:
                triage_result = await self.llm.generate(
                    prompt, system + "\n\n" + REINFORCED_SAFETY_RULES,
                    temperature=0.0, use_cache=False,
                )
                retry_triage = extract_json(triage_result.text)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("Triage retry failed, preserving original: %s", e)
                retry_triage = None

            if retry_triage is not None:
                triage = retry_triage  # retry succeeded → use it as new base

            summary_check2 = validate_output(triage.get("summary", ""))
            if not summary_check2.is_safe:
                log_block(
                    channel="triage", field_name="summary", result=summary_check2,
                    retry_attempted=True, fallback_applied=True,
                )
                triage["summary"] = FALLBACKS.triage_summary

        # Routing
        available = self.kb.get_specialties_for_symptoms(kb_matches or [s.get("name", "") for s in all_symptoms])
        available_str = json.dumps(
            [{"name": s["name"], "description": s["description"]} for s in available],
            ensure_ascii=False,
        )
        system, prompt = PromptTemplates.routing(
            json.dumps(triage, ensure_ascii=False), available_str
        )
        routing_result = await self.llm.generate(prompt, system, use_cache=False)
        try:
            routing = extract_json(routing_result.text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse routing: %s", routing_result.text)
            routing = {"specialists": [], "explanation": ""}

        # Output safety: validate explanation + each specialist reason.
        # Single retry on whole JSON if any user-facing field is unsafe;
        # then per-field fallback for whatever is still unsafe after retry.
        pre_retry_blocks: list[tuple[str, ValidationResult]] = []
        r_expl = validate_output(routing.get("explanation", ""))
        if not r_expl.is_safe:
            pre_retry_blocks.append(("explanation", r_expl))
        for i, s in enumerate(routing.get("specialists", [])):
            if isinstance(s, dict):
                r_reason = validate_output(s.get("reason", ""))
                if not r_reason.is_safe:
                    pre_retry_blocks.append((f"specialist_reason[{i}]", r_reason))

        if pre_retry_blocks:
            for field_name, vres in pre_retry_blocks:
                log_block(
                    channel="routing", field_name=field_name, result=vres,
                    retry_attempted=True, fallback_applied=False,
                )
            # On retry failure, preserve original routing — losing
            # specialists / specialty / priority just because the retry
            # JSON did not parse would be a safety regression.
            try:
                routing_result = await self.llm.generate(
                    prompt, system + "\n\n" + REINFORCED_SAFETY_RULES,
                    temperature=0.0, use_cache=False,
                )
                retry_routing = extract_json(routing_result.text)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("Routing retry failed, preserving original: %s", e)
                retry_routing = None

            if retry_routing is not None:
                routing = retry_routing  # retry succeeded → use it as new base
            r_expl2 = validate_output(routing.get("explanation", ""))
            if not r_expl2.is_safe:
                log_block(
                    channel="routing", field_name="explanation", result=r_expl2,
                    retry_attempted=True, fallback_applied=True,
                )
                routing["explanation"] = FALLBACKS.routing_explanation
            for i, s in enumerate(routing.get("specialists", [])):
                if isinstance(s, dict):
                    r_reason2 = validate_output(s.get("reason", ""))
                    if not r_reason2.is_safe:
                        log_block(
                            channel="routing", field_name=f"specialist_reason[{i}]",
                            result=r_reason2,
                            retry_attempted=True, fallback_applied=True,
                        )
                        s["reason"] = FALLBACKS.specialist_reason

        specialists = routing.get("specialists", [])

        # --- Post-LLM validation via KB symptoms_hint scoring ---
        symptom_keys = list(set(kb_matches))
        # Also try to extract keys from LLM-parsed symptoms
        for s in all_symptoms:
            name = s.get("name", "") if isinstance(s, dict) else str(s)
            for matched_key in self.kb.match_symptoms(name):
                if matched_key not in symptom_keys:
                    symptom_keys.append(matched_key)

        # Resolve LLM specialist names to keys
        llm_spec_keys = []
        for spec in specialists:
            spec_name = spec.get("specialty", "")
            resolved = self.kb._resolve_specialty_key(spec_name)
            if resolved:
                llm_spec_keys.append(resolved)

        if symptom_keys and llm_spec_keys:
            validation = self.kb.validate_llm_routing(llm_spec_keys, symptom_keys)

            if validation["warnings"]:
                logger.warning("LLM routing validation: %s", validation["warnings"])

            # If ALL LLM specialists score < 0.2, fall back to KB routing
            all_low = all(v["kb_score"] < 0.2 for v in validation["validated"])
            if all_low and validation["validated"]:
                kb_routing = self.kb.get_kb_routing(symptom_keys)
                if kb_routing:
                    logger.warning(
                        "Full KB fallback: LLM specialists all scored < 0.2, "
                        "using KB routing instead: %s", kb_routing
                    )
                    specialists = []
                    for item in kb_routing[:3]:
                        specialists.append({
                            "specialty": item["name"],
                            "reason": f"Рекомендация на основе анализа симптомов (совпадение: {item['weight']:.0%})",
                            "source": "kb_fallback",
                        })
                    routing["specialists"] = specialists
            else:
                # Add high-confidence KB suggestions that LLM missed
                for suggestion in validation["kb_suggestions"]:
                    if suggestion["weight"] >= 0.5:
                        specialists.append({
                            "specialty": suggestion["name"],
                            "reason": f"Дополнительная рекомендация (совпадение симптомов: {suggestion['weight']:.0%})",
                            "source": "kb_validation",
                        })
                routing["specialists"] = specialists

        # Generate personalized preparation via LLM based on actual symptoms
        triage_summary = triage.get("summary", "")
        specialists_for_prompt = json.dumps(
            [{"specialty": s.get("specialty", ""), "reason": s.get("reason", "")} for s in specialists],
            ensure_ascii=False,
        )
        system, prompt = PromptTemplates.preparation(
            specialists_for_prompt, symptoms_json, triage_summary
        )
        try:
            prep_result = await self.llm.generate(prompt, system, use_cache=False)
            prep_data = extract_json(prep_result.text)
            preparations = prep_data.get("preparations", {})
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("Failed to generate preparation, falling back to KB: %s", e)
            preparations = {}

        # Output safety: if any preparation item is unsafe, retry once with
        # reinforced rules. Any items still unsafe after retry are dropped;
        # the per-spec assignment below then falls through to KB static data.
        pre_retry_prep: list[tuple[str, ValidationResult]] = []
        for spec_name, items in (preparations or {}).items():
            for i, item in enumerate(items or []):
                if isinstance(item, str):
                    r = validate_output(item)
                    if not r.is_safe:
                        pre_retry_prep.append((f"preparations[{spec_name}][{i}]", r))

        if pre_retry_prep:
            for fname, vres in pre_retry_prep:
                log_block(
                    channel="preparation", field_name=fname, result=vres,
                    retry_attempted=True, fallback_applied=False,
                )
            try:
                prep_result = await self.llm.generate(
                    prompt, system + "\n\n" + REINFORCED_SAFETY_RULES,
                    temperature=0.0, use_cache=False,
                )
                prep_data = extract_json(prep_result.text)
                preparations = prep_data.get("preparations", {})
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("Preparation retry failed, will fall back to KB: %s", e)
                preparations = {}
            for spec_name in list(preparations.keys()):
                kept: list[str] = []
                for i, item in enumerate(preparations.get(spec_name) or []):
                    if not isinstance(item, str):
                        continue
                    r = validate_output(item)
                    if r.is_safe:
                        kept.append(item)
                    else:
                        log_block(
                            channel="preparation",
                            field_name=f"preparations[{spec_name}][{i}]",
                            result=r, retry_attempted=True, fallback_applied=True,
                        )
                preparations[spec_name] = kept

        for spec in specialists:
            spec_name = spec.get("specialty", "")
            llm_items = preparations.get(spec_name) if spec_name in preparations else None
            if llm_items:
                spec["preparation"] = llm_items
            else:
                # Fallback to KB static data when LLM didn't generate, all
                # items were filtered out, or the spec was absent from
                # the LLM response.
                for key, data in self.kb.specialties.items():
                    if data["name"] == spec_name:
                        spec["preparation"] = data.get("preparation", [])
                        break

        return {
            "triage": triage,
            "routing": routing,
            "specialists": specialists,
        }

    async def generate_pdf_data(self, session_state: dict) -> dict:
        """DEPRECATED since the PDF redesign (feat/pdf-redesign).

        The PDF download path no longer calls this method. It produced
        the LLM-enriched fields that the redesign drops from the PDF
        surface (complaints_simple, complaints_medical, questions_for_doctor,
        what_to_bring, timeline). The method is kept on disk for one
        iteration so a rollback of the PDF path stays a small diff; it
        will be removed in a follow-up chore PR once the redesign is
        validated in production.
        """
        symptoms_json = json.dumps(session_state.get("symptoms", []), ensure_ascii=False)
        triage_json = json.dumps(session_state.get("triage", {}), ensure_ascii=False)
        routing_json = json.dumps(session_state.get("routing", {}), ensure_ascii=False)

        system, prompt = PromptTemplates.pdf_summary(
            symptoms_json, triage_json, routing_json
        )
        result = await self.llm.generate(prompt, system, use_cache=False)
        try:
            parsed = extract_json(result.text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse PDF data: %s", result.text)
            return {
                "complaints_medical": "",
                "complaints_simple": session_state.get("triage", {}).get("summary", ""),
                "timeline": "",
                "questions_for_doctor": [],
                "what_to_bring": [],
            }

        # Output safety: validate four user-facing fields. Retry once on
        # any unsafe; per-field fallback for whatever is still unsafe.
        pre_retry: list[tuple[str, ValidationResult]] = []
        for fname in ("complaints_simple", "complaints_medical"):
            r = validate_output(parsed.get(fname, "") or "")
            if not r.is_safe:
                pre_retry.append((fname, r))
        for fname in ("questions_for_doctor", "what_to_bring"):
            for i, item in enumerate(parsed.get(fname, []) or []):
                if isinstance(item, str):
                    r = validate_output(item)
                    if not r.is_safe:
                        pre_retry.append((f"{fname}[{i}]", r))

        if pre_retry:
            for fname, vres in pre_retry:
                log_block(
                    channel="pdf_summary", field_name=fname, result=vres,
                    retry_attempted=True, fallback_applied=False,
                )
            # On retry failure: preserve the originally-parsed dict. Per-field
            # validation below will swap unsafe fields for their fallbacks
            # while leaving safe fields from the first call intact.
            try:
                result = await self.llm.generate(
                    prompt, system + "\n\n" + REINFORCED_SAFETY_RULES,
                    temperature=0.0, use_cache=False,
                )
                retry_parsed = extract_json(result.text)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("PDF summary retry failed, preserving original: %s", e)
                retry_parsed = None

            if retry_parsed is not None:
                parsed = retry_parsed  # retry succeeded → use it as new base
            # Per-field fallback for anything still unsafe.
            r_cs = validate_output(parsed.get("complaints_simple", "") or "")
            if not r_cs.is_safe:
                log_block(
                    channel="pdf_summary", field_name="complaints_simple", result=r_cs,
                    retry_attempted=True, fallback_applied=True,
                )
                parsed["complaints_simple"] = FALLBACKS.complaints_simple
            r_cm = validate_output(parsed.get("complaints_medical", "") or "")
            if not r_cm.is_safe:
                log_block(
                    channel="pdf_summary", field_name="complaints_medical", result=r_cm,
                    retry_attempted=True, fallback_applied=True,
                )
                parsed["complaints_medical"] = FALLBACKS.complaints_medical
            for fname, fallback_list in (
                ("questions_for_doctor", FALLBACKS.questions_for_doctor),
                ("what_to_bring", FALLBACKS.what_to_bring),
            ):
                items = parsed.get(fname, []) or []
                kept: list[str] = []
                any_blocked = False
                for i, item in enumerate(items):
                    if not isinstance(item, str):
                        continue
                    r = validate_output(item)
                    if r.is_safe:
                        kept.append(item)
                    else:
                        any_blocked = True
                        log_block(
                            channel="pdf_summary", field_name=f"{fname}[{i}]",
                            result=r, retry_attempted=True, fallback_applied=True,
                        )
                # Whole-list fallback only if every item was blocked.
                if not kept and any_blocked:
                    parsed[fname] = list(fallback_list)
                else:
                    parsed[fname] = kept

        return parsed
