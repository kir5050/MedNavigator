import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class MedicalKB:
    def __init__(self):
        kb_dir = Path(os.path.dirname(__file__))
        self.symptoms = self._load(kb_dir / "symptoms.yaml")["symptoms"]
        self.specialties = self._load(kb_dir / "specialties.yaml")["specialties"]
        self.red_flags = self._load(kb_dir / "red_flags.yaml")["red_flags"]
        self._synonym_index = self._build_synonym_index()
        self._red_flag_patterns = self._build_red_flag_patterns()

    @staticmethod
    def _load(path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _build_synonym_index(self) -> dict[str, str]:
        index = {}
        for key, data in self.symptoms.items():
            name_lower = data["name"].lower()
            index[name_lower] = key
            for syn in data.get("synonyms", []):
                index[syn.lower()] = key
        return index

    def _build_red_flag_patterns(self) -> list[tuple[str, str, bool]]:
        patterns = []
        for _category, data in self.red_flags.items():
            is_crisis = bool(data.get("is_crisis", False))
            for pattern in data["patterns"]:
                patterns.append((pattern.lower(), data["message"], is_crisis))
        return patterns

    def check_red_flags(self, text: str) -> dict | None:
        # Crisis matches dominate over physical red-flags regardless of YAML order:
        # a patient saying "хочу умереть от боли в груди" must hit the crisis path
        # (suicide hotline + session lock), not the cardiac message.
        text_lower = text.lower()
        first_non_crisis = None
        for pattern, message, is_crisis in self._red_flag_patterns:
            if pattern in text_lower:
                if is_crisis:
                    return {"message": message, "is_crisis": True}
                if first_non_crisis is None:
                    first_non_crisis = {"message": message, "is_crisis": False}
        return first_non_crisis

    def match_symptoms(self, text: str) -> list[str]:
        text_lower = text.lower()
        matched = set()
        for synonym, symptom_key in self._synonym_index.items():
            if synonym in text_lower:
                matched.add(symptom_key)
        return list(matched)

    def get_specialties_for_area(self, area: str) -> list[dict]:
        results = []
        for key, spec in self.specialties.items():
            if area in spec.get("areas", []):
                results.append({
                    "key": key,
                    "name": spec["name"],
                    "description": spec["description"],
                    "preparation": spec.get("preparation", []),
                })
        return results

    def get_specialties_for_symptoms(self, symptom_keys: list[str]) -> list[dict]:
        areas = set()
        for key in symptom_keys:
            if key in self.symptoms:
                areas.add(self.symptoms[key]["area"])

        results = []
        seen = set()
        for area in areas:
            for spec in self.get_specialties_for_area(area):
                if spec["key"] not in seen:
                    seen.add(spec["key"])
                    results.append(spec)
        return results

    def get_preparation(self, specialty_key: str) -> list[str]:
        spec = self.specialties.get(specialty_key)
        if spec:
            return spec.get("preparation", [])
        return []

    # --- KB-based routing validation (uses symptoms_hint from specialties.yaml) ---

    def _resolve_specialty_key(self, name: str) -> str | None:
        """Resolve specialist display name to key (e.g. 'Невролог' -> 'neurologist')."""
        for key, spec in self.specialties.items():
            if spec["name"] == name:
                return key
        return None

    def score_specialist(self, symptom_keys: list[str], specialist_key: str) -> float:
        """Score specialist relevance for given symptoms using symptoms_hint overlap.
        Returns 0.0–1.0.
        """
        spec = self.specialties.get(specialist_key)
        if not spec:
            return 0.0

        hints = spec.get("symptoms_hint", [])
        if not hints:
            return 0.0

        matched = len(set(symptom_keys) & set(hints))
        if matched == 0:
            return 0.0

        base_score = matched / len(hints)
        if matched >= 3:
            return min(round(base_score * 1.3, 2), 1.0)
        if matched >= 2:
            return min(round(base_score * 1.15, 2), 1.0)
        return round(base_score, 2)

    def get_kb_routing(self, symptom_keys: list[str]) -> list[dict]:
        """Score all specialists against symptoms, return sorted list."""
        results = []
        for key, spec in self.specialties.items():
            score = self.score_specialist(symptom_keys, key)
            if score > 0.0:
                results.append({
                    "specialist": key,
                    "name": spec["name"],
                    "weight": score,
                })
        results.sort(key=lambda x: x["weight"], reverse=True)
        return results

    def validate_llm_routing(
        self,
        llm_specialist_keys: list[str],
        symptom_keys: list[str],
    ) -> dict:
        """Validate LLM routing against KB symptom-based scoring.

        Returns:
            validated: each LLM specialist with its KB score
            warnings: specialists recommended by LLM but unsupported by KB
            kb_suggestions: high-scoring specialists that LLM missed
        """
        validated = []
        warnings = []

        for spec_key in llm_specialist_keys:
            score = self.score_specialist(symptom_keys, spec_key)
            validated.append({"specialist": spec_key, "kb_score": score})
            if score < 0.2:
                warnings.append(
                    f"LLM recommended '{spec_key}' but KB score is {score:.2f}"
                )

        kb_suggestions = []
        for item in self.get_kb_routing(symptom_keys):
            if item["weight"] >= 0.5 and item["specialist"] not in llm_specialist_keys:
                kb_suggestions.append(item)

        return {
            "validated": validated,
            "warnings": warnings,
            "kb_suggestions": kb_suggestions,
        }
