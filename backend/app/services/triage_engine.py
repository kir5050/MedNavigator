import json
import logging

from app.llm.manager import LLMManager
from app.medical_kb import MedicalKB
from app.prompts import PromptTemplates

logger = logging.getLogger(__name__)


class TriageEngine:
    MAX_CLARIFICATIONS = 4

    def __init__(self, llm: LLMManager, kb: MedicalKB):
        self.llm = llm
        self.kb = kb

    async def process_message(
        self, text: str, session_state: dict
    ) -> dict:
        # 1. Red flag check BEFORE anything else
        emergency = self.kb.check_red_flags(text)
        if emergency:
            return {
                "response": emergency,
                "status": "emergency",
                "is_emergency": True,
            }

        history = session_state.get("history", "")
        all_symptoms = session_state.get("symptoms", [])
        clarification_count = session_state.get("clarification_count", 0)

        # 2. Extract symptoms
        system, prompt = PromptTemplates.symptom_extraction(text, history)
        extraction = await self.llm.generate(prompt, system)
        try:
            extracted = json.loads(extraction.text)
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
            clarification = await self.llm.generate(prompt, system, use_cache=False)
            return {
                "response": clarification.text,
                "status": "collecting",
                "is_emergency": False,
                "symptoms": all_symptoms,
                "kb_matches": kb_matches,
                "clarification_count": clarification_count + 1,
            }

        # 4. Enough info collected — signal "ready" so user can trigger triage
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

        # KB matches from all collected symptoms
        kb_matches = []
        for s in all_symptoms:
            name = s.get("name", "") if isinstance(s, dict) else str(s)
            kb_matches.extend(self.kb.match_symptoms(name))

        # Triage
        symptoms_json = json.dumps(all_symptoms, ensure_ascii=False)
        system, prompt = PromptTemplates.triage(symptoms_json, history)
        triage_result = await self.llm.generate(prompt, system)
        try:
            triage = json.loads(triage_result.text)
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
        routing_result = await self.llm.generate(prompt, system)
        try:
            routing = json.loads(routing_result.text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse routing: %s", routing_result.text)
            routing = {"specialists": [], "explanation": ""}

        specialists = routing.get("specialists", [])

        # Add preparation from KB
        for spec in specialists:
            spec_name = spec.get("specialty", "")
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
            return json.loads(result.text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse PDF data: %s", result.text)
            return {
                "complaints_medical": "",
                "complaints_simple": session_state.get("triage", {}).get("summary", ""),
                "timeline": "",
                "questions_for_doctor": [],
                "what_to_bring": [],
            }
