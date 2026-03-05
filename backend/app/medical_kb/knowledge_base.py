import os
from pathlib import Path

import yaml


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

    def _build_red_flag_patterns(self) -> list[tuple[str, str]]:
        patterns = []
        for _category, data in self.red_flags.items():
            for pattern in data["patterns"]:
                patterns.append((pattern.lower(), data["message"]))
        return patterns

    def check_red_flags(self, text: str) -> str | None:
        text_lower = text.lower()
        for pattern, message in self._red_flag_patterns:
            if pattern in text_lower:
                return message
        return None

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
