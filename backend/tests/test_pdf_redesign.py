"""Regression tests for the post-redesign non-crisis PDF.

These tests are the regulatory regression line for the PDF surface:
they enforce both *what must appear* (the eight target sections from
docs/product/pdf-redesign-brief.md §3) and *what must NOT appear*
(a curated forbidden-substring list covering medical terminology,
old section headers, causal-association framing, and "outdated"
phrasings that were removed by the redesign).

The forbidden list applies only to a *controlled non-crisis HTML
fixture* — user-controlled fields (filenames, free-form patient turns)
must never be filtered by the runtime, only by the renderer's choice
to not show them.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.medical_kb.knowledge_base import MedicalKB
from app.pdf.generator import PDFGenerator
from app.pdf.view_model import (
    DISCLAIMER,
    URGENCY_TIMEFRAME_MAP,
    URGENT_CARE_BLOCK,
    build_view_model,
)


@pytest.fixture(scope="module")
def kb():
    return MedicalKB()


@pytest.fixture
def non_crisis_state():
    """Controlled non-crisis state for the forbidden-substring regression.
    Symptoms chosen so that the canonical KB names do NOT themselves
    contain any forbidden substring (e.g. "суставов") — otherwise the
    fixture would inject a false positive into the test."""
    return {
        "triage": {"urgency": "medium", "summary": "..."},
        "symptoms": [
            {"name": "болит голова"},   # → "Головная боль"
            {"name": "кашель"},          # → "Кашель"
            {"name": "тошнота"},         # → "Тошнота"
            {"name": "боль в горле"},   # → "Боль в горле"
        ],
        "routing": {
            "specialists": [
                {"specialty": "Терапевт", "reason": "ignored",
                 "preparation": ["ignored"]},
                {"specialty": "Лор", "reason": "ignored"},
            ],
            "explanation": "ignored",
        },
        "history_lines": ["Пациент: болит голова, кашель и тошнит"],
    }


# Brief §2 + extensions from review. Applied only against the controlled
# non-crisis fixture above. Reminder: this is regression-test infra, not a
# runtime denylist — the runtime denylist lives in output_safety.yaml.
FORBIDDEN_SUBSTRINGS = [
    "триаж",
    "триажа",
    "по данным триажа",
    "цефалгия",
    "артралгия",
    "медицинская выписка",
    "Информационная выписка",
    "медицинское заключение",
    "медицинским заключением",
    "предварительный диагноз",
    "Описание для врача",
    "Жалобы и симптомы",
    "Ход опроса",
    "Рекомендуемые специалисты",
    "Пояснение",
    "Вопросы для врача",
    "может быть связан",
    "может быть связана",
    "нервной системы",
    "суставов",
    "мягких тканей",
    "должны насторожить",
    "более срочного обращения",
    "по описанию",
]


# Target section headers / signal phrases (brief §3).
TARGET_SECTIONS = [
    "MedNavigator — маршрутный лист",
    "Помогает подготовиться к визиту и выбрать следующий шаг",
    "<h2>Рекомендуемый следующий шаг</h2>",
    "<h2>Что вы описали</h2>",
    "<h2>Также может пригодиться</h2>",
    "<h2>Подготовка к визиту</h2>",
    "<h2>Возможные вопросы врачу</h2>",
    "<h2>Когда нужно действовать срочнее</h2>",
]


class TestPDFRedesignContent:
    def test_all_target_sections_present(self, kb, non_crisis_state):
        vm = build_view_model(non_crisis_state, "def67890", kb)
        html = PDFGenerator._build_html(vm, "def67890")
        for marker in TARGET_SECTIONS:
            assert marker in html, f"target section missing: {marker!r}"

    @pytest.mark.parametrize("substring", FORBIDDEN_SUBSTRINGS)
    def test_forbidden_substring_absent(self, kb, non_crisis_state, substring):
        vm = build_view_model(non_crisis_state, "def67890", kb)
        html = PDFGenerator._build_html(vm, "def67890")
        assert substring not in html, (
            f"forbidden substring {substring!r} leaked into non-crisis HTML"
        )

    def test_primary_route_card_renders_specialty_and_urgency(self, kb, non_crisis_state):
        vm = build_view_model(non_crisis_state, "def67890", kb)
        html = PDFGenerator._build_html(vm, "def67890")
        # Primary specialty = first specialist from routing.
        assert '<span class="specialty">Терапевт</span>' in html
        # Urgency label from the static map, not the legacy banner.
        assert URGENCY_TIMEFRAME_MAP["medium"] in html
        # Safe one-liner included.
        assert "подходящий первый специалист для очной оценки" in html

    def test_secondary_route_mentioned_softly(self, kb, non_crisis_state):
        vm = build_view_model(non_crisis_state, "def67890", kb)
        html = PDFGenerator._build_html(vm, "def67890")
        assert "В зависимости от очной оценки врач может направить" in html
        assert "Лор" in html

    def test_what_patient_described_uses_canonical_names(self, kb, non_crisis_state):
        vm = build_view_model(non_crisis_state, "def67890", kb)
        html = PDFGenerator._build_html(vm, "def67890")
        # KB-canonical strings, not raw extraction.
        assert "Головная боль" in html
        assert "Кашель" in html
        assert "Тошнота" in html

    def test_demo_case_html_no_duplicates_no_raw_forms(self, kb):
        # Regression for the demo-case PDF reported on prod: raw synonym
        # of headache and a temperature-with-value were leaking into the
        # bullet list alongside the canonical labels. After this fix the
        # bullet list must be the clean canonical-only set.
        vm = build_view_model(
            {
                "triage": {"urgency": "medium"},
                "routing": {"specialists": [{"specialty": "Терапевт"}]},
                "symptoms": [
                    {"name": "Головная боль"},
                    {"name": "Кашель"},
                    {"name": "боль в голове"},
                    {"name": "температура 37.0"},
                ],
            },
            "def67890", kb,
        )
        html = PDFGenerator._build_html(vm, "def67890")

        # Canonical labels appear, each exactly once.
        assert html.count("<li>Головная боль</li>") == 1
        assert html.count("<li>Кашель</li>") == 1
        assert html.count("<li>Повышенная температура</li>") == 1
        # Raw forms must NOT leak into the rendered list.
        assert "боль в голове" not in html
        assert "температура 37.0" not in html
        assert "37.0" not in html

    def test_urgent_care_block_is_static_verbatim(self, kb, non_crisis_state):
        vm = build_view_model(non_crisis_state, "def67890", kb)
        html = PDFGenerator._build_html(vm, "def67890")
        assert URGENT_CARE_BLOCK in html
        assert "103 или 112" in html

    def test_disclaimer_is_compact_verbatim(self, kb, non_crisis_state):
        vm = build_view_model(non_crisis_state, "def67890", kb)
        html = PDFGenerator._build_html(vm, "def67890")
        assert DISCLAIMER in html
        # Old long-form disclaimer must not survive.
        assert "Данный документ носит исключительно информационный" not in html

    def test_no_crisis_banner_in_non_crisis_pdf(self, kb, non_crisis_state):
        vm = build_view_model(non_crisis_state, "def67890", kb)
        html = PDFGenerator._build_html(vm, "def67890")
        # Bare class name is in CSS for every PDF — check the rendered element.
        assert '<div class="crisis-banner">' not in html
        # Hotline must not appear unless the session is crisis-locked.
        assert "8-800-2000-122" not in html

    def test_full_pdf_render_does_not_crash(self, kb, non_crisis_state):
        vm = build_view_model(non_crisis_state, "def67890", kb)
        pdf_bytes = PDFGenerator.generate(vm, "def67890")
        assert isinstance(pdf_bytes, bytes)
        assert pdf_bytes.startswith(b"%PDF")
        assert len(pdf_bytes) > 1000


# ---------------------------------------------------------------------------
# Primary acceptance criterion: PDF download path makes 0 LLM calls.
# ---------------------------------------------------------------------------


import json
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock


@pytest_asyncio.fixture
async def client_with_mocked_llm():
    """Spin up the FastAPI app with in-memory SQLite and a mocked LLM.
    The LLM mock will fail the test instantly if any LLM call is made
    during the PDF request path (asserted in the individual tests)."""
    from app.main import app
    from app.medical_kb import MedicalKB
    from app.models.database import get_engine, get_session_maker
    from app.services.triage_engine import TriageEngine
    from app import main as main_module

    engine = await get_engine("sqlite+aiosqlite:///:memory:")
    session_maker = get_session_maker(engine)

    llm = MagicMock()
    llm.generate = AsyncMock(return_value=MagicMock(text='{"symptoms": []}'))
    triage = TriageEngine(llm=llm, kb=MedicalKB())

    main_module.db_session_maker = session_maker
    main_module.triage_engine = triage
    app.state.limiter.enabled = False

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, llm, session_maker

    app.state.limiter.enabled = True


async def _seed_completed_session(session_maker, *, crisis: bool, state_overrides: dict | None = None):
    """Insert a Session row that's ready for PDF download — status must be
    completed or emergency, state_json must carry enough for build_view_model."""
    from app.models.database import Session as DBSession
    base_state = {
        "history_lines": ["Пациент: болит голова и кашель"],
        "symptoms": [{"name": "болит голова"}, {"name": "кашель"}],
        "triage": {"urgency": "medium", "summary": "..."},
        "routing": {
            "specialists": [
                {"specialty": "Терапевт", "reason": "ignored",
                 "preparation": ["ignored"]},
            ],
            "explanation": "ignored",
        },
    }
    if crisis:
        base_state["crisis_locked"] = True
    if state_overrides:
        base_state.update(state_overrides)

    sess = DBSession(
        status="emergency" if crisis else "completed",
        state_json=json.dumps(base_state, ensure_ascii=False),
    )
    async with session_maker() as db:
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        return sess.id


class TestNoLLMCallsForPDF:
    """Primary acceptance criterion of the PDF redesign:
    GET /api/v1/session/{id}/pdf must NOT invoke the LLM."""

    @pytest.mark.asyncio
    async def test_non_crisis_pdf_request_zero_llm_calls(self, client_with_mocked_llm):
        client, llm, session_maker = client_with_mocked_llm
        sid = await _seed_completed_session(session_maker, crisis=False)

        r = await client.get(f"/api/v1/session/{sid}/pdf")
        assert r.status_code == 200
        assert r.content.startswith(b"%PDF")
        assert llm.generate.await_count == 0, (
            f"LLM was called {llm.generate.await_count} times during a "
            f"non-crisis PDF request — the redesign requires zero LLM calls "
            f"on the PDF download path."
        )

    @pytest.mark.asyncio
    async def test_crisis_pdf_request_zero_llm_calls(self, client_with_mocked_llm):
        # Defence-in-depth: even the crisis branch must not invoke the LLM.
        # build_view_model uses kb.check_red_flags (substring matcher, no LLM)
        # to populate the crisis red-flags section.
        client, llm, session_maker = client_with_mocked_llm
        sid = await _seed_completed_session(session_maker, crisis=True)

        r = await client.get(f"/api/v1/session/{sid}/pdf")
        assert r.status_code == 200
        assert r.content.startswith(b"%PDF")
        assert llm.generate.await_count == 0


class TestFileAttachmentsRemoved:
    """Smoke coverage for the file-attachment removal (feat/remove-file-attachments).

    Confirms the upload endpoint is gone and that the text-only flow
    (message → red-flag screening → routing → PDF) still works end to end
    with no file surface remaining.
    """

    @pytest.mark.asyncio
    async def test_upload_endpoint_removed(self, client_with_mocked_llm):
        client, _llm, session_maker = client_with_mocked_llm
        sid = await _seed_completed_session(session_maker, crisis=False)
        # The upload route no longer exists — FastAPI has no handler for it.
        r = await client.post(
            f"/api/v1/session/{sid}/upload",
            files={"file": ("x.txt", b"hello", "text/plain")},
        )
        assert r.status_code in (404, 405)

    @pytest.mark.asyncio
    async def test_text_message_redflag_screening(self, client_with_mocked_llm):
        # Red-flag screening runs on plain text input, pre-LLM. No file
        # branch is involved. A chest-pain phrase must trigger emergency.
        client, llm, _session_maker = client_with_mocked_llm
        start = await client.post("/api/v1/session/start")
        assert start.status_code in (200, 201)
        sid = start.json()["session_id"]

        r = await client.post(
            f"/api/v1/session/{sid}/message",
            json={"text": "у меня боль в груди"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["is_emergency"] is True
        # Red-flag is detected before any LLM call.
        assert llm.generate.await_count == 0

    @pytest.mark.asyncio
    async def test_text_only_session_produces_pdf(self, client_with_mocked_llm):
        # routing → PDF tail on a text-only session: a valid PDF is produced.
        client, _llm, session_maker = client_with_mocked_llm
        sid = await _seed_completed_session(session_maker, crisis=False)

        r = await client.get(f"/api/v1/session/{sid}/pdf")
        assert r.status_code == 200
        assert r.content.startswith(b"%PDF")
