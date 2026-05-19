"""Tests that the crisis branch of the PDF renderer continues to honour
the PR #11 contract after the PDF redesign:

- a clearly-visible crisis banner replaces the urgency block;
- the conversation history is rendered (so a parent / carer reviewing
  the PDF later sees the context that led to the crisis trigger);
- the bottom red-flags section carries the hotline message again;
- routine PDF sections (specialist card, checklists, urgent-care block)
  are NOT rendered for crisis-only sessions.

The view model contract has changed since PR #11 / PR #13 — these tests
build view models via ``build_view_model(state, sid, kb)`` and pass them
to the renderer. The original
``TestIsCrisisOnlyDerivation`` / ``TestRegularPDFUnchanged`` classes have
been retired: derivation is now covered by ``test_pdf_view_model.py``,
and the routine-PDF structure has changed substantially — its regression
suite lives in ``test_pdf_redesign.py``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.medical_kb.knowledge_base import MedicalKB
from app.pdf.generator import PDFGenerator
from app.pdf.view_model import build_view_model


# Visual contract markers preserved from PR #11.
CRISIS_BANNER_ELEMENT = '<div class="crisis-banner">'
CRISIS_TITLE = "Это срочное обращение, требующее немедленной помощи"
HOTLINE = "8-800-2000-122"
# Old urgency-banner string — must never appear in the crisis branch.
ROUTINE_MEDIUM_URGENCY = "Записаться в ближайшие"


@pytest.fixture(scope="module")
def kb():
    return MedicalKB()


@pytest.fixture
def crisis_view_model(kb):
    return build_view_model(
        {
            "crisis_locked": True,
            "history_lines": [
                "Пациент: хочу умереть",
                "Ассистент: Пожалуйста, обратитесь за помощью прямо сейчас. "
                "Телефон доверия: 8-800-2000-122 (бесплатно, круглосуточно). "
                "Скорая помощь: 103 или 112. Вы не одиноки, и помощь доступна.",
            ],
        },
        "abc12345",
        kb,
    )


class TestCrisisOnlyPDFRendering:
    def test_contains_crisis_banner(self, crisis_view_model):
        html = PDFGenerator._build_html(crisis_view_model, "abc12345")
        assert CRISIS_BANNER_ELEMENT in html
        assert CRISIS_TITLE in html
        assert HOTLINE in html
        assert "103 или 112" in html

    def test_hides_routine_urgency_banner(self, crisis_view_model):
        html = PDFGenerator._build_html(crisis_view_model, "abc12345")
        # The new routine layout uses a "Рекомендуемый следующий шаг" card
        # with an urgency label; neither must appear in the crisis branch.
        assert ROUTINE_MEDIUM_URGENCY not in html
        assert "<h2>Рекомендуемый следующий шаг</h2>" not in html

    def test_hides_routine_sections(self, kb):
        # A crisis state that happens to carry routine fields (e.g. an
        # intake session that produced a triage before the crisis trigger
        # fired) must NOT render any routine sections.
        vm = build_view_model(
            {
                "crisis_locked": True,
                "history_lines": ["Пациент: хочу умереть"],
                "symptoms": [{"name": "болит голова"}],
                "triage": {"urgency": "medium", "summary": "..."},
                "routing": {
                    "specialists": [
                        {"specialty": "Терапевт", "reason": "ignored",
                         "preparation": ["ignored"]},
                    ],
                    "explanation": "ignored",
                },
            },
            "abc12345", kb,
        )
        html = PDFGenerator._build_html(vm, "abc12345")
        # None of the new or old routine section headers should render.
        for header in (
            "<h2>Рекомендуемый следующий шаг</h2>",
            "<h2>Что вы описали</h2>",
            "<h2>Также может пригодиться</h2>",
            "<h2>Подготовка к визиту</h2>",
            "<h2>Возможные вопросы врачу</h2>",
            "<h2>Когда нужно действовать срочнее</h2>",
        ):
            assert header not in html, f"crisis PDF must not render {header!r}"
        # Old terminology that was retired by the redesign — also must not
        # appear in the crisis branch:
        assert "Жалобы и симптомы" not in html
        assert "Описание для врача" not in html
        assert "Рекомендуемые специалисты" not in html

    def test_keeps_history_documents_red_flags_and_disclaimer(self, kb):
        vm = build_view_model(
            {
                "crisis_locked": True,
                "history_lines": [
                    "Пациент: хочу умереть",
                    "Ассистент: Пожалуйста, обратитесь за помощью прямо сейчас. "
                    "Телефон доверия: 8-800-2000-122 (бесплатно, круглосуточно). "
                    "Скорая помощь: 103 или 112. Вы не одиноки, и помощь доступна.",
                ],
                "uploaded_files": [
                    {"filename": "anketa.pdf", "type": "application/pdf",
                     "analysis": "this text should NOT propagate to the PDF"},
                ],
            },
            "abc12345", kb,
        )
        html = PDFGenerator._build_html(vm, "abc12345")
        assert "<h2>Ход опроса</h2>" in html               # conversation log
        assert "anketa.pdf" in html                        # filename surfaces
        # Analysis text MUST NOT propagate after the redesign.
        assert "this text should NOT propagate" not in html
        assert "Внимание: тревожные признаки" in html     # red-flags section
        # Compact two-line disclaimer present (verbatim from view_model).
        assert "MedNavigator не ставит диагноз" in html

    def test_full_pdf_render_does_not_crash(self, crisis_view_model):
        pdf_bytes = PDFGenerator.generate(crisis_view_model, "abc12345")
        assert isinstance(pdf_bytes, bytes)
        assert pdf_bytes.startswith(b"%PDF")
        assert len(pdf_bytes) > 1000
