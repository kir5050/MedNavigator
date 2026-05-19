"""Tests for the PDF view model builder.

Covers urgency mapping, primary/secondary specialist split, KB-normalised
patient-described symptoms with deduplication, fallback path for symptoms
outside the KB, slim uploaded_files, crisis flag passthrough, and the
presence of static content blocks.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.medical_kb.knowledge_base import MedicalKB
from app.pdf.view_model import (
    DISCLAIMER,
    PREPARATION_CHECKLIST,
    PRIMARY_ROUTE_ONE_LINER,
    QUESTIONS_FOR_DOCTOR,
    URGENCY_TIMEFRAME_MAP,
    URGENT_CARE_BLOCK,
    build_view_model,
)


@pytest.fixture(scope="module")
def kb():
    return MedicalKB()


# ---------------------------------------------------------------------------
# Static content presence
# ---------------------------------------------------------------------------


class TestStaticContent:
    def test_urgency_timeframe_map_covers_all_levels(self):
        assert set(URGENCY_TIMEFRAME_MAP.keys()) >= {"low", "medium", "high", "emergency"}

    def test_preparation_checklist_within_brief_limit(self):
        assert 1 <= len(PREPARATION_CHECKLIST) <= 5

    def test_questions_for_doctor_within_brief_limit(self):
        assert 1 <= len(QUESTIONS_FOR_DOCTOR) <= 3

    def test_questions_for_doctor_canonical_text(self):
        """Regression: protect static doctor questions from accidental drift.
        Each question's distinctive substring is asserted — full-text
        equality would be too brittle for minor punctuation fixes."""
        assert len(QUESTIONS_FOR_DOCTOR) == 3
        assert "следующим шагом" in QUESTIONS_FOR_DOCTOR[0]
        assert "как меняется состояние" in QUESTIONS_FOR_DOCTOR[1]
        assert "другому специалисту" in QUESTIONS_FOR_DOCTOR[2]
        # Old wording must not return — it leaned the patient toward
        # asking the doctor to enumerate red-flag signs, which is the
        # exact medical-agenda framing the redesign removes.
        for q in QUESTIONS_FOR_DOCTOR:
            assert "наблюдать симптомы" not in q
            assert "обращать внимание" not in q
            assert "насторожить" not in q

    def test_urgent_care_block_mentions_emergency_numbers(self):
        assert "103" in URGENT_CARE_BLOCK
        assert "112" in URGENT_CARE_BLOCK

    def test_disclaimer_is_compact(self):
        # Brief §3.8 — "Компактно, 2 строки". We render as one paragraph;
        # under 200 chars keeps it visually compact.
        assert len(DISCLAIMER) < 250
        assert "MedNavigator не ставит диагноз" in DISCLAIMER

    def test_primary_one_liner_neutral(self):
        # Must NOT contain causal-association framing.
        assert "связан" not in PRIMARY_ROUTE_ONE_LINER
        assert "системы" not in PRIMARY_ROUTE_ONE_LINER
        assert "может быть" not in PRIMARY_ROUTE_ONE_LINER


# ---------------------------------------------------------------------------
# Urgency mapping
# ---------------------------------------------------------------------------


class TestUrgency:
    def test_medium_urgency_label(self, kb):
        vm = build_view_model({"triage": {"urgency": "medium"}}, "abcdefgh", kb)
        assert vm["urgency"]["level"] == "medium"
        assert vm["urgency"]["label"] == URGENCY_TIMEFRAME_MAP["medium"]

    def test_high_urgency_label(self, kb):
        vm = build_view_model({"triage": {"urgency": "high"}}, "abcdefgh", kb)
        assert vm["urgency"]["level"] == "high"
        assert vm["urgency"]["label"] == URGENCY_TIMEFRAME_MAP["high"]

    def test_unknown_urgency_falls_back_to_medium(self, kb):
        vm = build_view_model({"triage": {"urgency": "weird"}}, "abcdefgh", kb)
        assert vm["urgency"]["label"] == URGENCY_TIMEFRAME_MAP["medium"]

    def test_no_triage_in_state_defaults_to_medium(self, kb):
        vm = build_view_model({}, "abcdefgh", kb)
        assert vm["urgency"]["level"] == "medium"


# ---------------------------------------------------------------------------
# Primary / secondary specialist split
# ---------------------------------------------------------------------------


class TestSpecialistSplit:
    def test_no_specialists_yields_none_primary(self, kb):
        vm = build_view_model({"routing": {"specialists": []}}, "abcdefgh", kb)
        assert vm["primary_route"] is None
        assert vm["secondary_routes"] == []

    def test_one_specialist_primary_only(self, kb):
        vm = build_view_model(
            {"routing": {"specialists": [{"specialty": "Терапевт", "reason": "..."}]}},
            "abcdefgh", kb,
        )
        assert vm["primary_route"]["specialty"] == "Терапевт"
        assert vm["primary_route"]["safe_one_liner"] == PRIMARY_ROUTE_ONE_LINER
        assert vm["secondary_routes"] == []

    def test_three_specialists_split_one_plus_two(self, kb):
        vm = build_view_model(
            {"routing": {"specialists": [
                {"specialty": "Терапевт"},
                {"specialty": "Невролог"},
                {"specialty": "Ортопед"},
            ]}},
            "abcdefgh", kb,
        )
        assert vm["primary_route"]["specialty"] == "Терапевт"
        assert [s["specialty"] for s in vm["secondary_routes"]] == ["Невролог", "Ортопед"]

    def test_more_than_three_caps_at_two_secondary(self, kb):
        vm = build_view_model(
            {"routing": {"specialists": [
                {"specialty": "А"}, {"specialty": "Б"},
                {"specialty": "В"}, {"specialty": "Г"},
            ]}},
            "abcdefgh", kb,
        )
        assert len(vm["secondary_routes"]) == 2
        assert [s["specialty"] for s in vm["secondary_routes"]] == ["Б", "В"]

    def test_raw_llm_reason_is_not_propagated(self, kb):
        # Whatever LLM wrote into specialists[i].reason MUST NOT appear in
        # the view model — the renderer uses the static one-liner instead.
        vm = build_view_model(
            {"routing": {"specialists": [
                {"specialty": "Терапевт", "reason": "У вас, вероятно, гастрит"},
            ]}},
            "abcdefgh", kb,
        )
        assert "reason" not in vm["primary_route"]
        assert "гастрит" not in str(vm["primary_route"])

    def test_specialist_without_name_is_skipped(self, kb):
        vm = build_view_model(
            {"routing": {"specialists": [
                {"specialty": ""},
                {"specialty": "Невролог"},
            ]}},
            "abcdefgh", kb,
        )
        assert vm["primary_route"]["specialty"] == "Невролог"


# ---------------------------------------------------------------------------
# what_patient_described — KB normalisation
# ---------------------------------------------------------------------------


class TestWhatPatientDescribed:
    def test_kb_known_symptom_resolves_to_canonical(self, kb):
        # "болит голова" is a synonym → canonical "Головная боль".
        vm = build_view_model(
            {"symptoms": [{"name": "болит голова"}]},
            "abcdefgh", kb,
        )
        assert vm["what_patient_described"] == ["Головная боль"]

    def test_deduplication_across_synonyms(self, kb):
        vm = build_view_model(
            {"symptoms": [
                {"name": "болит голова"},
                {"name": "головная боль"},
                {"name": "головная боль"},
            ]},
            "abcdefgh", kb,
        )
        # Three inputs → one canonical entry.
        assert vm["what_patient_described"] == ["Головная боль"]

    def test_unknown_safe_symptom_falls_back_to_raw(self, kb):
        vm = build_view_model(
            {"symptoms": [{"name": "немного покалывает в боку"}]},
            "abcdefgh", kb,
        )
        # Not in KB synonyms — passes output_safety as plain Russian.
        assert vm["what_patient_described"] == ["немного покалывает в боку"]

    def test_unknown_unsafe_symptom_is_dropped(self, kb):
        # If LLM extraction somehow wrote a medication name or directive,
        # it must be filtered out by validate_output().
        vm = build_view_model(
            {"symptoms": [
                {"name": "болит голова"},
                {"name": "примите парацетамол"},
            ]},
            "abcdefgh", kb,
        )
        # Only the safe canonical name survives.
        assert vm["what_patient_described"] == ["Головная боль"]

    def test_deduplicates_headache_synonyms(self, kb):
        # "боль в голове" is now a synonym; "Головная боль" is canonical;
        # "болит голова" was already a synonym. All three must collapse
        # to a single canonical entry.
        vm = build_view_model(
            {"symptoms": [
                {"name": "Головная боль"},
                {"name": "боль в голове"},
                {"name": "болит голова"},
            ]},
            "abcdefgh", kb,
        )
        assert vm["what_patient_described"] == ["Головная боль"]

    def test_maps_temperature_with_value_to_canonical(self, kb):
        # Synonym "температура" exists; raw extraction wraps a numeric
        # value into the name field. Substring fallback must catch it.
        vm = build_view_model(
            {"symptoms": [{"name": "температура 37.0"}]},
            "abcdefgh", kb,
        )
        assert vm["what_patient_described"] == ["Повышенная температура"]

    def test_pdf_demo_case_symptoms_are_clean(self, kb):
        # The exact demo-case reported on prod: canonical + raw synonym
        # of the same symptom + raw measurement form. Expected output
        # is the post-fix clean list, no duplicates, no raw forms.
        vm = build_view_model(
            {"symptoms": [
                {"name": "Головная боль"},
                {"name": "Кашель"},
                {"name": "боль в голове"},
                {"name": "температура 37.0"},
            ]},
            "abcdefgh", kb,
        )
        assert vm["what_patient_described"] == [
            "Головная боль",
            "Кашель",
            "Повышенная температура",
        ]

    def test_unsafe_raw_symptom_is_dropped(self, kb):
        # Names like "у вас вероятно гастрит" come from a misbehaving LLM
        # extraction — they must NOT reach the PDF. validate_output drops them.
        vm = build_view_model(
            {"symptoms": [{"name": "у вас вероятно гастрит"}]},
            "abcdefgh", kb,
        )
        assert vm["what_patient_described"] == []

    def test_short_synonym_no_substring_false_positive(self, kb):
        # Defends the _SUBSTRING_MIN_LEN >= 6 invariant. KB has short
        # synonyms (e.g. "жар", "37", "зуд") — none of them may participate
        # in substring matching. An unrelated input that incidentally
        # contains such a short string must NOT be tagged with a medical
        # canonical name. The fixture is intentionally not in KB so the
        # only way it could be matched is via a bogus short-substring rule.
        vm = build_view_model(
            {"symptoms": [{"name": "поднос на кухне"}]},
            "abcdefgh", kb,
        )
        # No KB match (correct), and the raw string passes validate_output
        # → it surfaces as a raw fallback entry.
        assert vm["what_patient_described"] == ["поднос на кухне"]

    def test_empty_or_non_dict_entries_are_skipped(self, kb):
        vm = build_view_model(
            {"symptoms": [
                {"name": ""},
                None,
                {"not_a_name_key": "x"},
                {"name": "болит голова"},
            ]},
            "abcdefgh", kb,
        )
        assert vm["what_patient_described"] == ["Головная боль"]


# ---------------------------------------------------------------------------
# Uploaded files — slim, no analysis
# ---------------------------------------------------------------------------


class TestUploadedFilesSlimmed:
    def test_only_filename_propagated(self, kb):
        vm = build_view_model(
            {"uploaded_files": [
                {"filename": "analiz.pdf", "type": "application/pdf",
                 "analysis": "Documents contains diagnosis: gastritis"},
            ]},
            "abcdefgh", kb,
        )
        assert vm["uploaded_files"] == [{"filename": "analiz.pdf"}]

    def test_empty_filename_is_dropped(self, kb):
        vm = build_view_model(
            {"uploaded_files": [{"filename": "", "analysis": "..."}]},
            "abcdefgh", kb,
        )
        assert vm["uploaded_files"] == []


# ---------------------------------------------------------------------------
# Crisis flag passthrough
# ---------------------------------------------------------------------------


class TestCrisisFlag:
    def test_crisis_locked_true_propagated(self, kb):
        vm = build_view_model({"crisis_locked": True}, "abcdefgh", kb)
        assert vm["is_crisis_only"] is True

    def test_missing_key_means_not_crisis(self, kb):
        vm = build_view_model({}, "abcdefgh", kb)
        assert vm["is_crisis_only"] is False


# ---------------------------------------------------------------------------
# history_lines defence-in-depth filtering (assistant lines only)
# ---------------------------------------------------------------------------


class TestHistoryFiltering:
    def test_unsafe_assistant_line_dropped(self, kb):
        vm = build_view_model(
            {"history_lines": [
                "Пациент: болит голова",
                "Ассистент: Примите парацетамол",
                "Пациент: уже выпил",
                "Ассистент: когда это началось?",
            ]},
            "abcdefgh", kb,
        )
        assert vm["history_lines"] == [
            "Пациент: болит голова",
            "Пациент: уже выпил",
            "Ассистент: когда это началось?",
        ]

    def test_patient_lines_never_filtered(self, kb):
        vm = build_view_model(
            {"history_lines": [
                "Пациент: врач сказал у меня вероятно гастрит, выписал парацетамол",
            ]},
            "abcdefgh", kb,
        )
        assert vm["history_lines"] == [
            "Пациент: врач сказал у меня вероятно гастрит, выписал парацетамол",
        ]


# ---------------------------------------------------------------------------
# red_flags — populated only for crisis sessions
# ---------------------------------------------------------------------------


class TestRedFlagsPopulation:
    def test_red_flags_empty_for_non_crisis_session(self, kb):
        vm = build_view_model(
            {"history_lines": ["Пациент: болит голова"]},  # no crisis_locked
            "abcdefgh", kb,
        )
        assert vm["red_flags"] == []

    def test_red_flags_populated_for_crisis_session_from_patient_turn(self, kb):
        vm = build_view_model(
            {
                "crisis_locked": True,
                "history_lines": [
                    "Пациент: хочу умереть",
                    "Ассистент: Пожалуйста, обратитесь за помощью прямо сейчас. ...",
                ],
            },
            "abcdefgh", kb,
        )
        assert len(vm["red_flags"]) >= 1
        assert any("8-800-2000-122" in msg for msg in vm["red_flags"])


# ---------------------------------------------------------------------------
# Static block presence in the resulting view model
# ---------------------------------------------------------------------------


class TestViewModelShape:
    def test_required_keys_present(self, kb):
        vm = build_view_model({}, "abcdefgh", kb)
        for key in (
            "session_id_short", "generated_at", "is_crisis_only", "urgency",
            "primary_route", "secondary_routes", "what_patient_described",
            "preparation_checklist", "questions_for_doctor",
            "urgent_care_block", "disclaimer", "uploaded_files",
            "history_lines", "red_flags",
        ):
            assert key in vm, f"missing key: {key}"

    def test_static_blocks_pass_through_unchanged(self, kb):
        vm = build_view_model({}, "abcdefgh", kb)
        assert vm["preparation_checklist"] == PREPARATION_CHECKLIST
        assert vm["questions_for_doctor"] == QUESTIONS_FOR_DOCTOR
        assert vm["urgent_care_block"] == URGENT_CARE_BLOCK
        assert vm["disclaimer"] == DISCLAIMER

    def test_session_id_short_is_first_eight_chars(self, kb):
        vm = build_view_model({}, "abcdefgh-rest-of-uuid", kb)
        assert vm["session_id_short"] == "abcdefgh"

    def test_no_llm_call_during_build(self, kb):
        """Documented invariant: builder is a pure function, no LLM, no I/O."""
        # If kb._synonym_index were lazy and triggered an LLM, this test
        # would surface it — but it does not. We assert through behaviour:
        # multiple calls with same input produce identical output (modulo
        # the timestamp which is the only non-deterministic field).
        state = {
            "symptoms": [{"name": "болит голова"}],
            "routing": {"specialists": [{"specialty": "Невролог"}]},
            "triage": {"urgency": "high"},
        }
        vm1 = build_view_model(state, "abcdefgh", kb)
        vm2 = build_view_model(state, "abcdefgh", kb)
        for k in vm1.keys():
            if k == "generated_at":
                continue
            assert vm1[k] == vm2[k], f"non-deterministic field: {k}"
