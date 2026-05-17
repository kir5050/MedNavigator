"""Tests for crisis-only PDF cosmetics:
- is_crisis_only flag sourced from state["crisis_locked"] in pdf_data
- crisis-banner replaces routine urgency block
- intake-derived sections are hidden for crisis sessions
- non-crisis PDF rendering is unchanged"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pdf.generator import PDFGenerator


# Distinct markers to avoid CSS-leak false positives:
# the string "crisis-banner" lives in the <style> block too, so checks
# must look at the actual rendered element `<div class="crisis-banner">`,
# not just the bare class name.
CRISIS_BANNER_ELEMENT = '<div class="crisis-banner">'
CRISIS_TITLE = "Это срочное обращение, требующее немедленной помощи"
HOTLINE = "8-800-2000-122"
ROUTINE_MEDIUM_URGENCY = "Рекомендуется записаться в ближайшие 1-2 дня"


def _build_pdf_data_from_state(state: dict) -> dict:
    """Minimal reproduction of the pdf_data assembly in main.download_pdf.

    Only includes fields relevant to crisis-cosmetics; intentionally does
    not call the LLM enrichment (generate_pdf_data) — those LLM-derived
    fields are exercised separately and are not what these tests cover.
    """
    triage = state.get("triage", {})
    routing = state.get("routing", {})
    return {
        "complaints_medical": "",
        "complaints_simple": triage.get("summary", ""),
        "questions_for_doctor": [],
        "what_to_bring": [],
        "urgency": triage.get("urgency", "medium"),
        "symptoms_list": state.get("symptoms", []),
        "specialists": routing.get("specialists", []),
        "routing_explanation": routing.get("explanation", ""),
        "history_lines": state.get("history_lines", []),
        "uploaded_files": state.get("uploaded_files", []),
        "red_flags": [],
        "is_crisis_only": state.get("crisis_locked", False),
    }


class TestIsCrisisOnlyDerivation:
    """Step 1 of the pipeline: state["crisis_locked"] → pdf_data["is_crisis_only"]."""

    def test_crisis_session_yields_true(self):
        state = {"crisis_locked": True, "history_lines": ["Пациент: хочу умереть"]}
        assert _build_pdf_data_from_state(state)["is_crisis_only"] is True

    def test_regular_session_yields_false(self):
        state = {
            "history_lines": ["Пациент: болит голова"],
            "symptoms": [{"name": "головная боль"}],
            "triage": {"urgency": "medium", "summary": "..."},
            "routing": {"specialists": [{"specialty": "Терапевт"}]},
        }
        assert _build_pdf_data_from_state(state)["is_crisis_only"] is False

    def test_legacy_session_missing_crisis_locked_key_yields_false(self):
        """Sessions created before PR #8 have no crisis_locked key at all.
        The .get(..., False) default must produce False, never raise."""
        legacy_state = {
            "history_lines": ["Пациент: что-то старое"],
            "symptoms": [{"name": "кашель"}],
            # NB: no "crisis_locked" key
        }
        pdf_data = _build_pdf_data_from_state(legacy_state)
        assert pdf_data["is_crisis_only"] is False
        assert "is_crisis_only" in pdf_data  # always present, normalised to bool


class TestCrisisOnlyPDFRendering:
    """Step 2: HTML emitted for is_crisis_only=True sessions."""

    @pytest.fixture
    def crisis_data(self):
        return _build_pdf_data_from_state({
            "crisis_locked": True,
            "history_lines": [
                "Пациент: хочу умереть",
                "Ассистент: Пожалуйста, обратитесь за помощью прямо сейчас. "
                "Телефон доверия: 8-800-2000-122 (бесплатно, круглосуточно). "
                "Скорая помощь: 103 или 112. Вы не одиноки, и помощь доступна.",
            ],
        })

    def test_contains_crisis_banner(self, crisis_data):
        html = PDFGenerator._build_html(crisis_data, "abc12345")
        assert CRISIS_BANNER_ELEMENT in html
        assert CRISIS_TITLE in html
        assert HOTLINE in html
        assert "103 или 112" in html

    def test_hides_routine_urgency_banner(self, crisis_data):
        html = PDFGenerator._build_html(crisis_data, "abc12345")
        assert ROUTINE_MEDIUM_URGENCY not in html
        assert '<div class="urgency-box">' not in html

    def test_hides_intake_derived_sections(self, crisis_data):
        # Construct crisis_data with non-empty fields that would normally render
        # — proves the rendering is hidden even when LLM happens to produce content.
        crisis_data["complaints_simple"] = "должно быть скрыто"
        crisis_data["symptoms_list"] = [{"name": "что-то"}]
        crisis_data["specialists"] = [
            {"specialty": "Терапевт", "reason": "...", "preparation": ["x"]}
        ]
        crisis_data["routing_explanation"] = "должно быть скрыто"
        crisis_data["questions_for_doctor"] = ["q1", "q2"]
        crisis_data["what_to_bring"] = ["паспорт"]
        html = PDFGenerator._build_html(crisis_data, "abc12345")

        assert "<h2>Жалобы и симптомы</h2>" not in html
        assert "<h2>Рекомендуемые специалисты</h2>" not in html
        assert "<h2>Пояснение</h2>" not in html
        assert "<h2>Вопросы для врача</h2>" not in html
        assert "<h2>Что взять с собой</h2>" not in html
        # And the leaked field values do not appear elsewhere
        assert "должно быть скрыто" not in html

    def test_keeps_history_documents_red_flags_and_disclaimer(self, crisis_data):
        crisis_data["uploaded_files"] = [
            {"filename": "anketa.pdf", "type": "application/pdf",
             "analysis": "анализ файла, загруженного до crisis-trigger"}
        ]
        crisis_data["red_flags"] = [
            "Пожалуйста, обратитесь за помощью прямо сейчас. "
            "Телефон доверия: 8-800-2000-122 (бесплатно, круглосуточно). "
            "Скорая помощь: 103 или 112. Вы не одиноки, и помощь доступна."
        ]
        html = PDFGenerator._build_html(crisis_data, "abc12345")

        assert "<h2>Ход опроса</h2>" in html  # conversation log kept
        assert "anketa.pdf" in html  # uploaded files kept (pre-crisis context)
        assert "анализ файла, загруженного до crisis-trigger" in html
        assert "Внимание: тревожные признаки" in html  # red-flags section kept
        # disclaimer at the bottom of every PDF
        assert "информационный и справочный характер" in html

    def test_full_pdf_render_does_not_crash(self, crisis_data):
        """End-to-end: HTML → WeasyPrint → bytes. Catches CSS/HTML
        malformation that _build_html assertions would miss."""
        pdf_bytes = PDFGenerator.generate(crisis_data, "abc12345")
        assert isinstance(pdf_bytes, bytes)
        assert pdf_bytes.startswith(b"%PDF")
        assert len(pdf_bytes) > 1000  # sanity: non-empty document


class TestRegularPDFUnchanged:
    """Step 3: non-crisis PDFs render exactly as before this change."""

    @pytest.fixture
    def regular_data(self):
        return _build_pdf_data_from_state({
            "triage": {"urgency": "medium", "summary": "Жалобы на головную боль и кашель."},
            "symptoms": [{"name": "головная боль", "area": "head"}, {"name": "кашель"}],
            "routing": {
                "specialists": [
                    {"specialty": "Терапевт", "reason": "первичный осмотр",
                     "preparation": ["взять паспорт", "взять полис"]}
                ],
                "explanation": "Терапевт оценит общее состояние.",
            },
            "history_lines": ["Пациент: болит голова и кашель"],
        }) | {
            # LLM-derived fields populated as in a real run
            "questions_for_doctor": ["Как долго длится симптом?"],
            "what_to_bring": ["паспорт", "полис"],
        }

    def test_routine_urgency_banner_present(self, regular_data):
        html = PDFGenerator._build_html(regular_data, "def67890")
        assert '<div class="urgency-box">' in html
        assert ROUTINE_MEDIUM_URGENCY in html

    def test_intake_sections_present(self, regular_data):
        html = PDFGenerator._build_html(regular_data, "def67890")
        assert "<h2>Жалобы и симптомы</h2>" in html
        assert "<h2>Рекомендуемые специалисты</h2>" in html
        assert "<h2>Пояснение</h2>" in html
        assert "<h2>Вопросы для врача</h2>" in html
        assert "<h2>Что взять с собой</h2>" in html

    def test_no_crisis_banner_element(self, regular_data):
        """Critical: the rendered element <div class="crisis-banner"> must
        NOT appear. The bare string 'crisis-banner' is in the CSS for every
        PDF and is not a reliable signal."""
        html = PDFGenerator._build_html(regular_data, "def67890")
        assert CRISIS_BANNER_ELEMENT not in html
        assert CRISIS_TITLE not in html
        # No hotline number unless red_flags carries it
        assert HOTLINE not in html
