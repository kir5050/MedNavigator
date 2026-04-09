"""Tests for KB-based routing validation (score_specialist, get_kb_routing, validate_llm_routing)."""

import sys
from pathlib import Path

# Allow imports from backend/app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.medical_kb.knowledge_base import MedicalKB


def _kb() -> MedicalKB:
    return MedicalKB()


class TestScoreSpecialist:
    def test_neurologist_strong_match(self):
        kb = _kb()
        # headache, dizziness, numbness -> 3/5 hints for neurologist
        score = kb.score_specialist(["headache", "dizziness", "numbness"], "neurologist")
        assert score >= 0.7, f"Expected >= 0.7, got {score}"

    def test_cardiologist_two_symptoms(self):
        kb = _kb()
        score = kb.score_specialist(["chest_pain", "shortness_of_breath"], "cardiologist")
        assert score >= 0.5, f"Expected >= 0.5, got {score}"

    def test_dentist_single_symptom(self):
        kb = _kb()
        # toothache is the only hint for dentist -> 1/1 = 1.0
        score = kb.score_specialist(["toothache"], "dentist")
        assert score == 1.0, f"Expected 1.0, got {score}"

    def test_zero_for_unrelated(self):
        kb = _kb()
        score = kb.score_specialist(["headache", "dizziness"], "dentist")
        assert score == 0.0

    def test_zero_for_unknown_specialist(self):
        kb = _kb()
        score = kb.score_specialist(["headache"], "unknown_doctor")
        assert score == 0.0

    def test_therapist_no_hints(self):
        kb = _kb()
        # therapist has no symptoms_hint
        score = kb.score_specialist(["fever", "fatigue"], "therapist")
        assert score == 0.0


class TestGetKbRouting:
    def test_headache_dizziness_numbness(self):
        kb = _kb()
        routing = kb.get_kb_routing(["headache", "dizziness", "numbness"])
        assert len(routing) > 0
        assert routing[0]["specialist"] == "neurologist"

    def test_chest_pain_shortness(self):
        kb = _kb()
        routing = kb.get_kb_routing(["chest_pain", "shortness_of_breath"])
        specialist_keys = [r["specialist"] for r in routing]
        assert "cardiologist" in specialist_keys
        assert "pulmonologist" in specialist_keys

    def test_empty_symptoms(self):
        kb = _kb()
        routing = kb.get_kb_routing([])
        assert routing == []

    def test_sorted_by_weight_desc(self):
        kb = _kb()
        routing = kb.get_kb_routing(["headache", "dizziness", "numbness", "chest_pain"])
        weights = [r["weight"] for r in routing]
        assert weights == sorted(weights, reverse=True)


class TestValidateLlmRouting:
    def test_good_match_no_warnings(self):
        kb = _kb()
        result = kb.validate_llm_routing(
            ["neurologist"], ["headache", "dizziness", "numbness"]
        )
        assert len(result["warnings"]) == 0

    def test_bad_match_produces_warning(self):
        kb = _kb()
        result = kb.validate_llm_routing(
            ["dermatologist"], ["headache", "dizziness"]
        )
        assert len(result["warnings"]) == 1
        assert "dermatologist" in result["warnings"][0]

    def test_suggests_missed_specialist(self):
        kb = _kb()
        # LLM says therapist, but symptoms clearly point to neurologist
        result = kb.validate_llm_routing(
            ["therapist"], ["headache", "dizziness", "numbness"]
        )
        suggested_keys = [s["specialist"] for s in result["kb_suggestions"]]
        assert "neurologist" in suggested_keys

    def test_resolve_specialty_key(self):
        kb = _kb()
        assert kb._resolve_specialty_key("Невролог") == "neurologist"
        assert kb._resolve_specialty_key("Кардиолог") == "cardiologist"
        assert kb._resolve_specialty_key("Несуществующий") is None
