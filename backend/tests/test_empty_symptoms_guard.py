"""Tests for empty/nonsense symptoms guard in TriageEngine."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.triage_engine import TriageEngine


def _make_engine():
    """Create TriageEngine with mocked LLM."""
    llm = MagicMock()
    # Default: LLM returns no symptoms
    llm.generate = AsyncMock(return_value=MagicMock(
        text='{"symptoms": [], "needs_clarification": true}'
    ))

    from app.medical_kb.knowledge_base import MedicalKB
    kb = MedicalKB()
    return TriageEngine(llm=llm, kb=kb)


class TestNonsenseInputRejection:
    """Fix #4: reject obviously non-text input before calling LLM."""

    @pytest.mark.asyncio
    async def test_digits_only(self):
        engine = _make_engine()
        result = await engine.process_message("123", {})
        assert result["status"] == "collecting"
        assert "опишите словами" in result["response"]
        # LLM should NOT have been called
        engine.llm.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_symbols_only(self):
        engine = _make_engine()
        result = await engine.process_message("!@#$%^", {})
        assert result["status"] == "collecting"
        assert "опишите словами" in result["response"]

    @pytest.mark.asyncio
    async def test_single_letter_rejected(self):
        engine = _make_engine()
        result = await engine.process_message("a", {})
        assert "опишите словами" in result["response"]

    @pytest.mark.asyncio
    async def test_short_valid_answer_passes(self):
        """'да' and 'нет' should pass the filter (2+ letters)."""
        engine = _make_engine()
        result = await engine.process_message("да", {})
        # Should proceed to LLM extraction, not be rejected
        engine.llm.generate.assert_called()

    @pytest.mark.asyncio
    async def test_real_symptoms_pass(self):
        engine = _make_engine()
        engine.llm.generate = AsyncMock(return_value=MagicMock(
            text='{"symptoms": [{"name": "головная боль", "area": "head"}], "needs_clarification": true}'
        ))
        result = await engine.process_message("болит голова", {})
        assert result["status"] == "collecting"
        assert "опишите словами" not in result["response"]


class TestEmptySymptomsAfterClarifications:
    """Fix #1: don't say 'ready' when no symptoms were extracted."""

    @pytest.mark.asyncio
    async def test_no_symptoms_after_max_clarifications(self):
        engine = _make_engine()
        # Simulate: MAX_CLARIFICATIONS reached, but no symptoms collected
        session = {
            "symptoms": [],
            "history": "some history",
            "clarification_count": TriageEngine.MAX_CLARIFICATIONS,
        }
        result = await engine.process_message("ещё какой-то текст", session)
        # Should NOT be "ready" — no symptoms
        assert result["status"] == "collecting"
        assert "не смог определить" in result["response"]
        # Symptoms and counter should be reset
        assert result["symptoms"] == []

    @pytest.mark.asyncio
    async def test_with_symptoms_after_clarifications_is_ready(self):
        engine = _make_engine()
        session = {
            "symptoms": [{"name": "головная боль", "area": "head"}, {"name": "тошнота", "area": "abdomen"}],
            "history": "some history",
            "clarification_count": TriageEngine.MAX_CLARIFICATIONS,
        }
        # LLM returns no NEW symptoms but we already have enough
        result = await engine.process_message("больше ничего", session)
        assert result["status"] == "ready"


class TestRunTriageGuard:
    """Fix #2: run_triage refuses to proceed without real symptoms."""

    @pytest.mark.asyncio
    async def test_empty_symptoms_returns_safe_response(self):
        engine = _make_engine()
        result = await engine.run_triage({"symptoms": [], "history": ""})
        assert result["specialists"] == []
        assert result["triage"]["urgency"] == "low"
        assert "не удалось" in result["triage"]["summary"].lower()
        # LLM should NOT have been called
        engine.llm.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_garbage_symptoms_returns_safe_response(self):
        engine = _make_engine()
        result = await engine.run_triage({
            "symptoms": [{"not_a_name": "123"}],
            "history": "",
        })
        assert result["specialists"] == []
        engine.llm.generate.assert_not_called()
