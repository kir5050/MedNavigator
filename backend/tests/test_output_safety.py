"""Tests for output_safety validator, safe_generate_text wrapper,
PDF preflight pass, and the parametrised UNSAFE / SAFE regression sets.

Per-channel integration tests (call sites in triage_engine, main.send_message,
main.download_pdf) live alongside this file and are added by the commits
that wire those call sites up.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import output_safety as os_mod
from app.services.output_safety import (
    FALLBACKS,
    OutputSafetyValidator,
    REINFORCED_SAFETY_RULES,
    ValidationResult,
    preflight_pdf_data,
    safe_generate_text,
    validate_output,
)


# Critical regression sets supplied by product.
# If any UNSAFE string is not blocked, or any SAFE string is blocked,
# the test fails — these are the demo-risk cases the YAML must catch
# and the over-trigger cases it must NOT.

UNSAFE_CASES = [
    # Diagnoses — claim attribution
    "У вас, вероятно, гастрит.",
    "У вас вероятно гастрит.",
    "У вас, возможно, гастрит.",
    "У вас возможно гастрит.",
    "По описанию это похоже на гастрит.",
    "Симптомы указывают на гастрит.",
    "Можно предположить гастрит.",
    "У вас может быть мигрень.",
    "Возможно, у вас простуда.",
    "Похоже, у вас ОРВИ.",
    "Есть подозрение на бронхит.",
    "Это может свидетельствовать о язвенной болезни.",
    # Medications — case forms (demo-risk pattern)
    "Попробуйте Но-шпу.",
    "Выпейте Нурофен после еды.",
    "Примите парацетамол.",
    "Рекомендую таблетку парацетамола.",
    "Помогает анальгин или ибупрофен.",
    "Обработайте йодом.",
    "Можно нафтизин или називин.",
    # Dosages
    "Принимайте 500 мг парацетамола 2 раза в день.",
    "Выпейте 1 таблетку утром.",
    "По 2 капсулы каждые 6 часов.",
    "0,5 мг утром.",
    "Курс лечения 7 дней.",
    "На голодный желудок.",
    "Раствор 2%.",
    # Directive verbs
    "Полощите горло раствором.",
    "Смажьте ранку.",
    "Промывайте нос солевым раствором.",
    # ICD codes
    "Код по МКБ K29.3.",
    "По МКБ-10 это К21.0.",
    "Код заболевания К29.3.",
]


SAFE_CASES = [
    # Navigation / clarification (no claim, no prescription)
    "Когда у вас начались симптомы?",
    "Обратитесь к врачу для очной оценки.",
    "Врач может рекомендовать дальнейшие шаги после осмотра.",
    "Попробуйте записаться к терапевту.",
    "Попробуйте описать, когда началось.",
    "Вам поможет очная консультация врача.",
    "Загружен файл с результатами анализов.",
    # Source-attributed history (NOT AI claim)
    "Пациент сообщил, что ранее врач ставил диагноз гастрит.",
    "Пациент указал, что гастрит был диагностирован ранее.",
    "В документе указано ранее поставленное врачом заключение.",
    # Generic explanations (must not over-trigger)
    "Это может быть связано с режимом сна.",
    "Это симптом, который стоит обсудить с врачом.",
    "Характерно для разных состояний.",
]


@pytest.fixture(autouse=True)
def _reset_validator_each_test():
    # The validator caches compiled patterns at module level. Reset between
    # tests so a test mocking the validator does not leak into the next one.
    yield
    os_mod.reset_validator_for_tests()


# ---------------------------------------------------------------------------
# Validator basics
# ---------------------------------------------------------------------------


class TestValidatorBasics:
    def test_safe_text_returns_is_safe_true(self):
        r = validate_output("Когда у вас начались симптомы?")
        assert r.is_safe is True
        assert r.matched_categories == []
        assert r.matched_pattern_ids == []
        assert r.matched_pattern_count == 0

    def test_empty_string_is_safe(self):
        r = validate_output("")
        assert r.is_safe is True

    def test_none_safe_string_is_caught(self):
        r = validate_output("Примите парацетамол")
        assert r.is_safe is False
        assert "medications" in r.matched_categories
        assert "directive_verbs" in r.matched_categories
        assert r.matched_pattern_count >= 2

    def test_yaml_loads_at_init_without_error(self):
        v = OutputSafetyValidator()
        # Just calling validate() proves the YAML and all regexes compile.
        v.validate("test")


# ---------------------------------------------------------------------------
# UNSAFE / SAFE regression sets — the spec
# ---------------------------------------------------------------------------


class TestUnsafeMustBlock:
    @pytest.mark.parametrize("text", UNSAFE_CASES)
    def test_blocked(self, text):
        r = validate_output(text)
        assert r.is_safe is False, (
            f"UNSAFE text was not blocked: {text!r}\n"
            f"This is a demo-risk failure: this exact formulation must trigger the filter."
        )
        assert r.matched_pattern_ids, "blocked but no pattern_ids — validator bug"


class TestSafeMustPass:
    @pytest.mark.parametrize("text", SAFE_CASES)
    def test_not_blocked(self, text):
        r = validate_output(text)
        assert r.is_safe is True, (
            f"SAFE text was wrongly blocked: {text!r}\n"
            f"Matched: categories={r.matched_categories} ids={r.matched_pattern_ids}\n"
            f"This is a false-positive regression — the listed phrase must remain allowed."
        )


# ---------------------------------------------------------------------------
# Match types — surface-level invariants
# ---------------------------------------------------------------------------


class TestMatchTypes:
    def test_substring_match_case_insensitive(self):
        # "у вас, вероятно," is substring; case should not matter
        assert validate_output("У вас, вероятно, что-то есть").is_safe is False
        assert validate_output("у вас, вероятно, что-то есть").is_safe is False

    def test_word_match_respects_word_boundary(self):
        # "примите" is a word-type pattern.
        assert validate_output("примите таблетку").is_safe is False
        # It should NOT trigger inside an unrelated longer token.
        assert validate_output("приметаемое").is_safe is True

    def test_regex_dosage_decimal_with_comma_and_dot(self):
        assert validate_output("0,5 мг").is_safe is False
        assert validate_output("2.5 мл").is_safe is False


class TestCategoryDetection:
    def test_one_text_two_categories(self):
        # "Примите парацетамол" hits directive_verbs (примите) and medications (парацетамол).
        r = validate_output("Примите парацетамол")
        assert set(r.matched_categories) >= {"directive_verbs", "medications"}

    def test_icd_code_latin_and_cyrillic(self):
        assert validate_output("K29.3").is_safe is False  # Latin K
        assert validate_output("К29.3").is_safe is False  # Cyrillic К


# ---------------------------------------------------------------------------
# safe_generate_text — retry / fallback flow
# ---------------------------------------------------------------------------


def _resp(text: str):
    """Build an object with a .text attribute, matching LLMResponse contract."""
    m = MagicMock()
    m.text = text
    return m


@pytest.mark.asyncio
class TestSafeGenerateText:
    async def test_first_call_safe_returns_text(self):
        call = AsyncMock(side_effect=[_resp("Когда у вас начались симптомы?")])
        out = await safe_generate_text(
            call, system="sys", channel="chat", field_name="clarification",
            fallback=FALLBACKS.chat_clarification,
        )
        assert out == "Когда у вас начались симптомы?"
        assert call.await_count == 1

    async def test_first_unsafe_retry_safe_returns_retry_text(self):
        call = AsyncMock(side_effect=[
            _resp("Примите парацетамол"),
            _resp("Когда это началось?"),
        ])
        out = await safe_generate_text(
            call, system="sys", channel="chat", field_name="clarification",
            fallback=FALLBACKS.chat_clarification,
        )
        assert out == "Когда это началось?"
        assert call.await_count == 2

    async def test_first_unsafe_retry_unsafe_returns_fallback(self):
        call = AsyncMock(side_effect=[
            _resp("Примите парацетамол"),
            _resp("Выпейте 500 мг ибупрофена"),
        ])
        out = await safe_generate_text(
            call, system="sys", channel="chat", field_name="clarification",
            fallback="<fallback>",
        )
        assert out == "<fallback>"
        assert call.await_count == 2

    async def test_retry_uses_temperature_zero_and_reinforced_system(self):
        call = AsyncMock(side_effect=[
            _resp("Примите парацетамол"),
            _resp("Безопасный ответ."),
        ])
        await safe_generate_text(
            call, system="базовый system", channel="x", field_name="y",
            fallback="fb", base_temperature=0.5,
        )
        first_call_kwargs = call.await_args_list[0].kwargs
        second_call_kwargs = call.await_args_list[1].kwargs
        assert first_call_kwargs["temperature"] == 0.5
        assert first_call_kwargs["use_cache"] is False
        assert second_call_kwargs["temperature"] == 0.0
        assert second_call_kwargs["use_cache"] is False
        # Retry system arg contains the reinforced rules.
        second_system_arg = call.await_args_list[1].args[0]
        assert "ЗАПРЕЩЁННЫЕ" in second_system_arg
        assert REINFORCED_SAFETY_RULES.split("\n")[0] in second_system_arg

    async def test_logger_does_not_leak_llm_text(self, caplog):
        import logging as _logging
        caplog.set_level(_logging.WARNING)
        call = AsyncMock(side_effect=[
            _resp("По описанию это похоже на гастрит."),
            _resp("Тоже похоже на гастрит."),
        ])
        await safe_generate_text(
            call, system="sys", channel="chat", field_name="clarification",
            fallback="fb",
        )
        joined = " ".join(rec.getMessage() for rec in caplog.records)
        # The unsafe LLM output substrings must not appear in logs.
        assert "По описанию это похоже" not in joined
        assert "гастрит" not in joined
        # But category names and pattern ids are project config — safe.
        assert "diagnoses" in joined


# ---------------------------------------------------------------------------
# PDF preflight — strip unsafe persisted LLM-derived content
# ---------------------------------------------------------------------------


class TestPDFPreflight:
    def test_old_unsafe_assistant_history_line_omitted_from_pdf_data(self):
        pdf_data = {
            "history_lines": [
                "Пациент: болит голова",
                "Ассистент: Примите парацетамол 500 мг",   # unsafe
                "Пациент: уже выпил",
                "Ассистент: когда это началось?",          # safe
            ],
        }
        out = preflight_pdf_data(pdf_data)
        # Patient lines preserved, unsafe assistant line dropped, safe assistant line kept.
        assert out["history_lines"] == [
            "Пациент: болит голова",
            "Пациент: уже выпил",
            "Ассистент: когда это началось?",
        ]

    def test_patient_lines_never_filtered_even_if_they_contain_safety_terms(self):
        # The patient may legitimately recount what a doctor told them or
        # which medication they took. Those are the patient's own words and
        # must never be filtered.
        pdf_data = {
            "history_lines": [
                "Пациент: врач сказал, у меня вероятно гастрит, выписал парацетамол 500 мг",
                "Ассистент: когда это началось?",
            ],
        }
        out = preflight_pdf_data(pdf_data)
        assert out["history_lines"][0].startswith("Пациент: ")
        assert "парацетамол" in out["history_lines"][0]

    def test_old_unsafe_uploaded_files_analysis_becomes_empty(self):
        pdf_data = {
            "uploaded_files": [
                {"filename": "x.jpg", "type": "image/jpeg",
                 "analysis": "Документ содержит назначение: Нурофен 200 мг"},
            ],
        }
        out = preflight_pdf_data(pdf_data)
        # filename and type preserved, analysis emptied.
        assert out["uploaded_files"][0]["filename"] == "x.jpg"
        assert out["uploaded_files"][0]["type"] == "image/jpeg"
        assert out["uploaded_files"][0]["analysis"] == ""

    def test_safe_uploaded_files_analysis_preserved(self):
        pdf_data = {
            "uploaded_files": [
                {"filename": "x.jpg", "analysis": "документ содержит данные анализов"},
            ],
        }
        out = preflight_pdf_data(pdf_data)
        assert out["uploaded_files"][0]["analysis"] == "документ содержит данные анализов"

    def test_unsafe_top_level_field_replaced_with_fallback(self):
        pdf_data = {"routing_explanation": "У вас, вероятно, гастрит"}
        out = preflight_pdf_data(pdf_data)
        assert out["routing_explanation"] == FALLBACKS.routing_explanation

    def test_complaints_medical_hidden_on_unsafe(self):
        pdf_data = {"complaints_medical": "У вас может быть мигрень"}
        out = preflight_pdf_data(pdf_data)
        # FALLBACKS.complaints_medical is "" — section gets hidden in generator.
        assert out["complaints_medical"] == ""

    def test_specialist_reason_replaced_preparation_items_dropped(self):
        pdf_data = {
            "specialists": [
                {
                    "specialty": "Терапевт",
                    "reason": "Можно предположить гастрит",  # unsafe
                    "preparation": [
                        "вспомните, когда начались жалобы",   # safe
                        "примите парацетамол перед визитом",  # unsafe
                    ],
                },
            ],
        }
        out = preflight_pdf_data(pdf_data)
        spec = out["specialists"][0]
        assert spec["specialty"] == "Терапевт"
        assert spec["reason"] == FALLBACKS.specialist_reason
        assert spec["preparation"] == ["вспомните, когда начались жалобы"]

    def test_questions_for_doctor_fully_replaced_when_all_unsafe(self):
        pdf_data = {
            "questions_for_doctor": [
                "Нужно ли принять парацетамол?",
                "По описанию это похоже на язву — нужно ли обследоваться?",
            ],
        }
        out = preflight_pdf_data(pdf_data)
        # All items blocked → list replaced wholesale with the generic fallback.
        assert out["questions_for_doctor"] == FALLBACKS.questions_for_doctor

    def test_questions_for_doctor_kept_when_some_safe(self):
        pdf_data = {
            "questions_for_doctor": [
                "Нужно ли принять парацетамол?",  # unsafe
                "Что может быть следующим шагом?",  # safe
            ],
        }
        out = preflight_pdf_data(pdf_data)
        # Mixed: kept items only, no wholesale fallback.
        assert out["questions_for_doctor"] == ["Что может быть следующим шагом?"]

    def test_what_to_bring_fallback_policy_mirrors_questions(self):
        pdf_data = {"what_to_bring": ["Принесите парацетамол", "купите нурофен"]}
        out = preflight_pdf_data(pdf_data)
        assert out["what_to_bring"] == FALLBACKS.what_to_bring

    def test_preflight_does_not_mutate_input(self):
        pdf_data = {"routing_explanation": "У вас, вероятно, гастрит"}
        out = preflight_pdf_data(pdf_data)
        assert pdf_data["routing_explanation"] == "У вас, вероятно, гастрит"
        assert out is not pdf_data


# ---------------------------------------------------------------------------
# Chat channel — process_message clarification path
# ---------------------------------------------------------------------------


def _make_triage_engine(llm_response_text: str | list[str]):
    """Build TriageEngine with a mock LLM that returns predetermined text.
    Pass a list to model successive responses (first call, retry, etc.).
    """
    from app.medical_kb.knowledge_base import MedicalKB
    from app.services.triage_engine import TriageEngine

    llm = MagicMock()
    if isinstance(llm_response_text, str):
        llm.generate = AsyncMock(return_value=_resp(llm_response_text))
    else:
        llm.generate = AsyncMock(side_effect=[_resp(t) for t in llm_response_text])
    return TriageEngine(llm=llm, kb=MedicalKB())


@pytest.mark.asyncio
class TestChatClarificationFilter:
    async def test_unsafe_clarification_retry_safe_returns_retry_text(self):
        # symptom_extraction returns enough symptoms to reach clarification stage,
        # then clarification LLM call returns unsafe first, safe on retry.
        engine = _make_triage_engine([
            # 1st call: symptom_extraction
            '{"symptoms": [{"name": "головная боль"}], "needs_clarification": true}',
            # 2nd call: clarification (unsafe)
            "Примите парацетамол при головной боли.",
            # 3rd call: retry clarification (safe)
            "Когда у вас начались эти ощущения?",
        ])
        result = await engine.process_message("болит голова", {})
        assert result["status"] == "collecting"
        assert result["response"] == "Когда у вас начались эти ощущения?"

    async def test_unsafe_clarification_retry_unsafe_returns_fallback(self):
        engine = _make_triage_engine([
            '{"symptoms": [{"name": "головная боль"}], "needs_clarification": true}',
            "Примите парацетамол при головной боли.",
            "Выпейте нурофен по 1 таблетке.",  # retry also unsafe
        ])
        result = await engine.process_message("болит голова", {})
        assert result["status"] == "collecting"
        assert result["response"] == FALLBACKS.chat_clarification

    async def test_safe_clarification_passes_through_unchanged(self):
        engine = _make_triage_engine([
            '{"symptoms": [{"name": "головная боль"}], "needs_clarification": true}',
            "Расскажите, как давно это происходит.",
        ])
        result = await engine.process_message("болит голова", {})
        assert result["response"] == "Расскажите, как давно это происходит."
        # LLM called twice (extraction + clarification), no retry.
        assert engine.llm.generate.await_count == 2


# ---------------------------------------------------------------------------
# file_analysis prompt — regression on regulatory framing
# ---------------------------------------------------------------------------


class TestFileAnalysisPrompt:
    """The inline prompt for file analysis (upload endpoint) must no longer
    request ICD codes / diagnoses verbatim. This is a regression test
    against accidental revert of the regulatory tightening."""

    def test_prompt_does_not_request_icd_codes(self):
        # Inspect the source of main.py to confirm the prompt no longer
        # contains the "Диагнозы, если указаны (код МКБ)" line.
        # We compare on substring of the rendered prompt string.
        from app import main as main_module
        source = Path(main_module.__file__).read_text(encoding="utf-8")
        # Old line removed:
        assert "Диагнозы, если указаны (код МКБ)" not in source
        # New neutral framing present:
        assert "НЕ копируй и НЕ называй диагнозы" in source
        assert "документ содержит ранее поставленное врачом заключение" in source


# ---------------------------------------------------------------------------
# _build_documents_section — disclaimer + filename-without-analysis
# ---------------------------------------------------------------------------


class TestDocumentsSectionAlwaysDisclaimer:
    def test_empty_uploaded_files_returns_empty(self):
        from app.pdf.generator import PDFGenerator
        assert PDFGenerator._build_documents_section({"uploaded_files": []}) == ""

    def test_disclaimer_present_when_files_with_safe_analysis(self):
        from app.pdf.generator import PDFGenerator
        html = PDFGenerator._build_documents_section({
            "uploaded_files": [
                {"filename": "a.pdf", "analysis": "документ содержит данные анализов"},
            ],
        })
        assert "docs-disclaimer" in html
        assert "врач увидит оригиналы на приёме" in html
        assert "a.pdf" in html
        assert "документ содержит данные анализов" in html

    def test_disclaimer_present_when_files_have_empty_analysis(self):
        from app.pdf.generator import PDFGenerator
        html = PDFGenerator._build_documents_section({
            "uploaded_files": [
                {"filename": "b.jpg", "analysis": ""},
            ],
        })
        # Even with no analysis text, the upload itself is preserved (filename)
        # and the disclaimer is rendered. The fact of upload is information.
        assert "docs-disclaimer" in html
        assert "b.jpg" in html

    def test_no_analysis_paragraph_when_analysis_empty(self):
        from app.pdf.generator import PDFGenerator
        html = PDFGenerator._build_documents_section({
            "uploaded_files": [
                {"filename": "c.pdf", "analysis": ""},
            ],
        })
        # Filename in <h3>, but no <p> with analysis content
        assert "<h3>c.pdf</h3>" in html
        # No paragraph paragraph after the filename card
        # (presence of any '<p>' inside the per-file card)
        per_file_card_start = html.index("<h3>c.pdf</h3>")
        card_remainder = html[per_file_card_start:]
        assert "<p>" not in card_remainder.split("</div>")[0]
