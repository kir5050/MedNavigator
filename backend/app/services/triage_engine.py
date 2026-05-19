import json
import logging
import re

from app.llm.manager import LLMManager
from app.medical_kb import MedicalKB
from app.prompts import PromptTemplates
from app.services.output_safety import FALLBACKS, safe_generate_text

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

        # 1.5. Reject obviously non-text input (digits, symbols only)
        if len(self._NONSENSE_RE.findall(text)) < 2:
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

        for spec in specialists:
            spec_name = spec.get("specialty", "")
            if spec_name in preparations:
                spec["preparation"] = preparations[spec_name]
            else:
                # Fallback to KB static data only if LLM didn't generate
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
        symptoms_json = json.dumps(session_state.get("symptoms", []), ensure_ascii=False)
        triage_json = json.dumps(session_state.get("triage", {}), ensure_ascii=False)
        routing_json = json.dumps(session_state.get("routing", {}), ensure_ascii=False)

        # Include uploaded file analyses for richer PDF
        uploaded_files = session_state.get("uploaded_files", [])
        files_summary = ""
        if uploaded_files:
            analyses = [f.get("analysis", "") for f in uploaded_files if f.get("analysis")]
            if analyses:
                files_summary = "\n".join(analyses)

        system, prompt = PromptTemplates.pdf_summary(
            symptoms_json, triage_json, routing_json, files_summary
        )
        result = await self.llm.generate(prompt, system, use_cache=False)
        try:
            return extract_json(result.text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse PDF data: %s", result.text)
            return {
                "complaints_medical": "",
                "complaints_simple": session_state.get("triage", {}).get("summary", ""),
                "timeline": "",
                "questions_for_doctor": [],
                "what_to_bring": [],
            }
