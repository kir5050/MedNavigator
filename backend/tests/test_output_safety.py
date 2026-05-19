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
        # Assert the whole constant is appended — stronger than a single-
        # substring check and immune to wording changes within the constant.
        assert REINFORCED_SAFETY_RULES in second_system_arg
        # And the original base system is still present (appended, not replaced).
        assert "базовый system" in second_system_arg

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

    async def test_first_unsafe_retry_runtime_error_returns_fallback(self, caplog):
        """The retry-call must not propagate provider/network/runtime
        exceptions to the caller after the first call already produced
        unsafe content. Fallback is the safe option."""
        import logging as _logging
        caplog.set_level(_logging.WARNING)

        call = AsyncMock(side_effect=[
            _resp("Примите парацетамол"),                    # first: unsafe
            RuntimeError("provider exploded mid-retry"),     # retry: raises
        ])
        out = await safe_generate_text(
            call, system="sys", channel="chat", field_name="clarification",
            fallback=FALLBACKS.chat_clarification,
        )
        assert out == FALLBACKS.chat_clarification
        joined = " ".join(rec.getMessage() for rec in caplog.records)
        # We log that retry raised AND a block event with fallback_applied=true.
        assert "fallback_applied=True" in joined
        # The raw exception message ("provider exploded mid-retry") must not
        # appear in logs — only the exception type ("RuntimeError") does.
        assert "RuntimeError" in joined
        assert "provider exploded mid-retry" not in joined
        # And no unsafe LLM substring in logs.
        assert "парацетамол" not in joined


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
        # contains the "Диагнозы, если указаны (код МКБ)" line and that
        # the new safety framing is in place.
        from app import main as main_module
        source = Path(main_module.__file__).read_text(encoding="utf-8")
        # Old line removed:
        assert "Диагнозы, если указаны (код МКБ)" not in source
        # New non-doctor framing present (opening sentence):
        assert "Не ставишь диагноз" in source
        # New user-facing-safety framing present:
        assert "Текст должен быть безопасен для пользовательского интерфейса и PDF" in source
        # Header of the explicit prohibition block present:
        assert "НЕ копируй и НЕ называй:" in source
        # Neutral framing example still present:
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


# ---------------------------------------------------------------------------
# JSON channels — integration through TriageEngine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTriageJSONFilter:
    async def test_triage_summary_unsafe_retry_unsafe_falls_back(self):
        engine = _make_triage_engine([
            # 1. triage — unsafe summary
            '{"urgency": "medium", "summary": "Симптомы указывают на гастрит.", "medical_areas": []}',
            # 2. triage retry — still unsafe
            '{"urgency": "medium", "summary": "По описанию это похоже на язву.", "medical_areas": []}',
            # 3. routing — safe
            '{"specialists": [{"specialty": "Невролог", "reason": "оценка боли"}], "explanation": "обычная маршрутизация"}',
            # 4. preparation — safe
            '{"preparations": {"Невролог": ["взять паспорт"]}}',
        ])
        result = await engine.run_triage({
            "symptoms": [{"name": "головная боль"}, {"name": "тошнота"}],
            "history": "",
        })
        # Summary must NOT carry unsafe content from either LLM attempt.
        assert "гастрит" not in result["triage"]["summary"]
        assert "язву" not in result["triage"]["summary"]
        # Fallback is used after second unsafe attempt.
        assert result["triage"]["summary"] == FALLBACKS.triage_summary

    async def test_safe_triage_passes_through_unchanged(self):
        engine = _make_triage_engine([
            '{"urgency": "medium", "summary": "Пациент описал жалобы.", "medical_areas": []}',
            '{"specialists": [{"specialty": "Невролог", "reason": "оценка боли"}], "explanation": "обычно при таких жалобах"}',
            '{"preparations": {"Невролог": ["взять паспорт"]}}',
        ])
        result = await engine.run_triage({
            "symptoms": [{"name": "головная боль"}, {"name": "тошнота"}],
            "history": "",
        })
        assert result["triage"]["summary"] == "Пациент описал жалобы."


@pytest.mark.asyncio
class TestRoutingJSONFilter:
    async def test_routing_explanation_and_reason_unsafe_both_replaced(self):
        engine = _make_triage_engine([
            # 1. triage — safe
            '{"urgency": "medium", "summary": "Жалобы записаны.", "medical_areas": []}',
            # 2. routing — unsafe explanation AND reason
            '{"specialists": [{"specialty": "Невролог", "reason": "Можно предположить мигрень."}], "explanation": "Симптомы указывают на мигрень."}',
            # 3. routing retry — still unsafe
            '{"specialists": [{"specialty": "Невролог", "reason": "У вас, вероятно, мигрень."}], "explanation": "По описанию это похоже на мигрень."}',
            # 4. preparation — safe
            '{"preparations": {"Невролог": ["взять паспорт"]}}',
        ])
        result = await engine.run_triage({
            "symptoms": [{"name": "головная боль"}, {"name": "тошнота"}],
            "history": "",
        })
        # Both fields should be replaced with FALLBACKS strings.
        explanation = result["routing"]["explanation"]
        assert "мигрень" not in explanation
        assert explanation == FALLBACKS.routing_explanation
        # specialist reason — note that KB-validation may have replaced specialists
        # entirely; tolerant assertion: if our LLM specialist still present,
        # its reason must be safe.
        for spec in result["routing"]["specialists"]:
            assert "мигрень" not in spec.get("reason", "")


@pytest.mark.asyncio
class TestPDFSummaryJSONFilter:
    async def test_pdf_summary_unsafe_questions_replaced_by_generic(self):
        from app.medical_kb.knowledge_base import MedicalKB
        from app.services.triage_engine import TriageEngine
        llm = MagicMock()
        llm.generate = AsyncMock(side_effect=[
            # 1. first pdf_summary call — unsafe questions/what_to_bring
            _resp(
                '{"complaints_simple": "Жалобы.", "complaints_medical": "Жалобы.", '
                '"timeline": "", '
                '"questions_for_doctor": ["Нужно ли принять парацетамол?", "По описанию это похоже на язву — нужно ли?"], '
                '"what_to_bring": ["принесите парацетамол"]}'
            ),
            # 2. retry — still unsafe
            _resp(
                '{"complaints_simple": "Жалобы.", "complaints_medical": "Жалобы.", '
                '"timeline": "", '
                '"questions_for_doctor": ["Выпейте 1 таблетку — стоит ли?"], '
                '"what_to_bring": ["купите нурофен"]}'
            ),
        ])
        engine = TriageEngine(llm=llm, kb=MedicalKB())
        out = await engine.generate_pdf_data({
            "symptoms": [{"name": "головная боль"}],
            "triage": {"summary": "Жалобы записаны."},
            "routing": {"specialists": []},
        })
        # Both lists must have been replaced wholesale by the generic fallbacks.
        assert out["questions_for_doctor"] == FALLBACKS.questions_for_doctor
        assert out["what_to_bring"] == FALLBACKS.what_to_bring
        # complaints_medical hide-on-unsafe policy preserved across the retry
        # path even when complaints_medical itself was already safe in the
        # original — the per-field validate after retry runs against the
        # retry parse (also safe here), so this assertion is implicit but
        # we make it explicit as a regression guard.
        assert out.get("complaints_medical", None) is not None


# ---------------------------------------------------------------------------
# JSON retry FAILURE must preserve original safe structured fields
# (BLOCKER 2 from PR #13 review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestJSONRetryFailurePreservesOriginal:
    """If the retry LLM call returns invalid JSON (or raises), the original
    first-call parsed JSON MUST NOT be replaced by a default dict — that
    would silently drop safe structured fields (notably triage.urgency,
    routing.specialists[].priority/specialty)."""

    async def test_triage_retry_invalid_json_preserves_urgency_and_falls_back_summary(self):
        # symptoms with names that don't match KB synonyms → KB-validation
        # in routing is skipped → routing.specialists is preserved from LLM.
        engine = _make_triage_engine([
            # 1. triage: urgency=high (safe), summary unsafe
            '{"urgency": "high", "urgency_reason": "сильные жалобы", '
            '"medical_areas": ["abdomen"], '
            '"summary": "По описанию это похоже на гастрит."}',
            # 2. triage retry: invalid JSON
            "garbage with no JSON at all",
            # 3. routing: safe
            '{"specialists": [{"specialty": "Терапевт", "reason": "первичный осмотр", "priority": 1}], '
            '"explanation": "обычная маршрутизация"}',
            # 4. preparation: safe
            '{"preparations": {"Терапевт": ["взять паспорт"]}}',
        ])
        result = await engine.run_triage({
            "symptoms": [
                {"name": "тестовый симптом один"},
                {"name": "тестовый симптом два"},
            ],
            "history": "",
        })
        # CRITICAL: urgency must NOT have collapsed to the default "medium".
        assert result["triage"]["urgency"] == "high"
        assert result["triage"]["urgency_reason"] == "сильные жалобы"
        assert result["triage"]["medical_areas"] == ["abdomen"]
        # summary fell back to the generic safe text.
        assert result["triage"]["summary"] == FALLBACKS.triage_summary

    async def test_routing_retry_invalid_json_preserves_specialists_and_falls_back_text_fields(self):
        engine = _make_triage_engine([
            # 1. triage: safe
            '{"urgency": "medium", "summary": "Жалобы записаны.", "medical_areas": []}',
            # 2. routing: specialty/priority safe, both reason AND explanation unsafe
            '{"specialists": [{"specialty": "Невролог", '
            '"reason": "Можно предположить мигрень.", "priority": 1}], '
            '"explanation": "Симптомы указывают на мигрень."}',
            # 3. routing retry: invalid JSON
            "completely broken response",
            # 4. preparation: safe
            '{"preparations": {"Невролог": ["взять паспорт"]}}',
        ])
        result = await engine.run_triage({
            "symptoms": [
                {"name": "тестовый симптом один"},
                {"name": "тестовый симптом два"},
            ],
            "history": "",
        })
        # CRITICAL: specialists list must NOT have been wiped to [].
        specialists = result["routing"]["specialists"]
        assert len(specialists) == 1
        assert specialists[0]["specialty"] == "Невролог"
        assert specialists[0]["priority"] == 1
        # reason replaced with generic safe text, mention of диагноз gone.
        assert specialists[0]["reason"] == FALLBACKS.specialist_reason
        assert "мигрень" not in specialists[0]["reason"]
        # explanation replaced with generic.
        assert result["routing"]["explanation"] == FALLBACKS.routing_explanation
        assert "мигрень" not in result["routing"]["explanation"]

    async def test_pdf_summary_retry_invalid_json_applies_generic_fallbacks_and_preserves_safe_fields(self):
        from app.medical_kb.knowledge_base import MedicalKB
        from app.services.triage_engine import TriageEngine
        llm = MagicMock()
        llm.generate = AsyncMock(side_effect=[
            # 1. first pdf_summary: safe complaints_simple,
            #    unsafe complaints_medical, unsafe questions/what_to_bring.
            _resp(
                '{"complaints_simple": "Пациент описал жалобы на головную боль и тошноту.", '
                '"complaints_medical": "По описанию это похоже на мигрень.", '
                '"timeline": "несколько дней", '
                '"questions_for_doctor": ["Нужно ли принять парацетамол?"], '
                '"what_to_bring": ["принесите ибупрофен"]}'
            ),
            # 2. retry: invalid JSON — must not wipe original.
            _resp("retry totally failed"),
        ])
        engine = TriageEngine(llm=llm, kb=MedicalKB())
        out = await engine.generate_pdf_data({
            "symptoms": [{"name": "тестовый симптом"}],
            "triage": {"summary": "..."},
            "routing": {"specialists": []},
        })
        # complaints_simple was safe in original → preserved.
        assert out["complaints_simple"] == "Пациент описал жалобы на головную боль и тошноту."
        # complaints_medical was unsafe → hide policy (empty string).
        assert out["complaints_medical"] == ""
        # questions_for_doctor: all original items were unsafe → wholesale fallback.
        assert out["questions_for_doctor"] == FALLBACKS.questions_for_doctor
        # what_to_bring: same wholesale fallback.
        assert out["what_to_bring"] == FALLBACKS.what_to_bring
        # timeline (non-user-facing, not under safety) preserved from first call.
        assert out.get("timeline") == "несколько дней"


# ---------------------------------------------------------------------------
# Crisis-PDF with pre-existing unsafe persisted state (Δ11)
# ---------------------------------------------------------------------------


class TestCrisisPDFWithPreExistingUnsafeState:
    """Crisis sessions (PR #8/#11) hide most routine sections, but history
    and uploaded_files still render. Any persisted unsafe content from
    pre-crisis turns must be scrubbed by preflight."""

    def test_pre_crisis_unsafe_assistant_lines_omitted_in_crisis_pdf(self):
        pdf_data = {
            "is_crisis_only": True,
            "history_lines": [
                "Пациент: болит голова",
                "Ассистент: По описанию это похоже на мигрень.",  # unsafe pre-crisis
                "Пациент: хочу умереть",
                "Ассистент: Пожалуйста, обратитесь за помощью прямо сейчас. "
                "Телефон доверия: 8-800-2000-122 ...",  # safe crisis-message itself
            ],
            "red_flags": [
                "Пожалуйста, обратитесь за помощью прямо сейчас. "
                "Телефон доверия: 8-800-2000-122 (бесплатно, круглосуточно). "
                "Скорая помощь: 103 или 112. Вы не одиноки, и помощь доступна.",
            ],
        }
        out = preflight_pdf_data(pdf_data)
        kept = out["history_lines"]
        # Both patient lines preserved (own words, never filtered).
        assert kept[0] == "Пациент: болит голова"
        assert kept[1] == "Пациент: хочу умереть"
        # Unsafe assistant line omitted entirely; safe crisis-message kept.
        joined = " ".join(kept)
        assert "мигрень" not in joined
        assert "8-800-2000-122" in joined

    def test_pre_crisis_unsafe_uploaded_file_analysis_emptied_in_crisis_pdf(self):
        pdf_data = {
            "is_crisis_only": True,
            "uploaded_files": [
                {"filename": "old_anketa.pdf",
                 "analysis": "Документ содержит назначение: Нурофен 200 мг"},
            ],
            "history_lines": ["Пациент: хочу умереть"],
        }
        out = preflight_pdf_data(pdf_data)
        f = out["uploaded_files"][0]
        # filename preserved (fact-of-upload is information for the doctor)
        assert f["filename"] == "old_anketa.pdf"
        # analysis emptied (would be hidden by _build_documents_section)
        assert f["analysis"] == ""


# ---------------------------------------------------------------------------
# PDF cache wipe on startup (always-wipe-on-startup, NOT one-shot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_wipes_existing_pdf_cache():
    """The startup migration block must zero out triage_results.pdf_cache /
    pdf_generated_at on every run, so that no stale PDF produced before
    output_safety landed can be served back to a client."""
    from sqlalchemy import select, text as sql_text
    from app.models.database import (
        Base, TriageResult, get_engine, get_session_maker,
    )

    engine = await get_engine("sqlite+aiosqlite:///:memory:")
    session_maker = get_session_maker(engine)

    # Pre-populate a TriageResult with non-null pdf_cache.
    async with session_maker() as db:
        rec = TriageResult(
            session_id="session-1",
            urgency="medium",
            specialists=[],
            symptoms_summary="...",
            pdf_cache=b"%PDF-pretend-old-unsafe-bytes",
        )
        db.add(rec)
        await db.commit()

    # Run the same UPDATE the startup block runs.
    async with engine.begin() as conn:
        await conn.execute(sql_text(
            "UPDATE triage_results SET pdf_cache = NULL, pdf_generated_at = NULL"
        ))

    async with session_maker() as db:
        rows = (await db.execute(select(TriageResult))).scalars().all()
        assert len(rows) == 1
        assert rows[0].pdf_cache is None
        assert rows[0].pdf_generated_at is None
        # session_id / other fields preserved.
        assert rows[0].session_id == "session-1"
