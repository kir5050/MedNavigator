"""Tests for crisis flow: suicide/self-harm detection, classification, and session lock."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.medical_kb.knowledge_base import MedicalKB


# All 37 suicide/self-harm patterns from red_flags.yaml > suicidal.
# Kept inline (not imported from YAML) so a regression that silently drops
# a pattern would fail this test rather than passing against the new shorter list.
SUICIDE_PATTERNS = [
    # Direct suicide phrasings (Russian)
    "суицид",
    "суицидальн",
    "самоубийств",
    "покончить с собой",
    "покончить с жизнью",
    "покончить со всем",
    "свести счёты с жизнью",
    "свести счеты с жизнью",
    "наложить на себя руки",
    "убить себя",
    "убью себя",
    "хочу умереть",
    "хочу сдохнуть",
    "не хочу жить",
    "жить не хочу",
    "жить не хочется",
    "не хочется жить",
    "лучше бы я умер",
    "лучше бы я умерла",
    "лучше бы меня не было",
    "зачем мне жить",
    "нет смысла жить",
    "устал жить",
    "устала жить",
    "больше не могу жить",
    "уйти из жизни",
    # English (mixed-input safety net)
    "suicide",
    "suicidal",
    "kill myself",
    "end my life",
    "kms",
    # Self-harm (same crisis response)
    "причинить себе вред",
    "навредить себе",
    "резать себя",
    "порезать себя",
    "вскрыть вены",
    "вскрою вены",
]


@pytest.fixture(scope="module")
def kb():
    return MedicalKB()


# ---------------------------------------------------------------------------
# KB-level classification tests (sync, no LLM, no DB)
# ---------------------------------------------------------------------------


class TestSuicidePatternsTriggerCrisis:
    @pytest.mark.parametrize("phrase", SUICIDE_PATTERNS)
    def test_each_pattern_returns_crisis(self, kb, phrase):
        flag = kb.check_red_flags(phrase)
        assert flag is not None, f"pattern {phrase!r} did not trigger any red-flag"
        assert flag["is_crisis"] is True, f"pattern {phrase!r} triggered non-crisis branch"
        assert "8-800-2000-122" in flag["message"]

    def test_documented_false_positive_safe_direction(self, kb):
        # "не хочу умереть" contains the literal substring "хочу умереть".
        # This is an accepted over-trigger in the safety direction:
        # the cost of asking a non-crisis user to see the hotline is
        # negligible; the cost of missing a real crisis is not.
        flag = kb.check_red_flags("не хочу умереть")
        assert flag is not None
        assert flag["is_crisis"] is True


class TestNonCrisisRedFlagsStaySafe:
    """Physical red-flags must still fire with is_crisis=False — they are not regressed."""

    @pytest.mark.parametrize(
        "phrase,must_contain",
        [
            ("у меня боль в груди", "103"),
            ("я задыхаюсь", "103"),
            ("у меня обморок", "103"),
            ("онемела половина лица", "инсульт"),
            ("сильное кровотечение", "103"),
            ("температура 40", "40"),
        ],
    )
    def test_non_crisis_pattern(self, kb, phrase, must_contain):
        flag = kb.check_red_flags(phrase)
        assert flag is not None
        assert flag["is_crisis"] is False
        assert must_contain in flag["message"]
        # And — critically — no hotline number, so we know it's the right branch
        assert "8-800-2000-122" not in flag["message"]


class TestCrisisDominatesOverPhysicalRedFlag:
    """Compound utterances (crisis + physical symptom) must route to crisis,
    independent of YAML ordering of red-flag categories."""

    @pytest.mark.parametrize(
        "compound_phrase",
        [
            "хочу умереть от боли в груди",           # crisis + cardiac
            "не могу дышать, не хочу жить",            # breathing + crisis
            "у меня обморок и хочу умереть",          # consciousness + crisis
            "много крови, я больше не могу жить",      # bleeding + crisis
        ],
    )
    def test_crisis_wins(self, kb, compound_phrase):
        flag = kb.check_red_flags(compound_phrase)
        assert flag is not None
        assert flag["is_crisis"] is True, (
            f"compound {compound_phrase!r} returned non-crisis branch; "
            "crisis-priority logic in check_red_flags is broken"
        )
        assert "8-800-2000-122" in flag["message"]


class TestNoMatchReturnsNone:
    def test_neutral_input(self, kb):
        assert kb.check_red_flags("болит ухо несколько дней") is None


# ---------------------------------------------------------------------------
# Endpoint-level integration tests for session lock (async, in-memory DB)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client_with_locked_llm():
    """Spin up the real FastAPI app against in-memory SQLite with a mocked LLM.

    The LLM mock returns one extracted symptom so normal intake (non-crisis)
    progresses past the empty-symptoms guard. Crisis trigger does not consume
    the LLM (red-flag check fires before extraction).
    """
    from app.main import app
    from app.medical_kb import MedicalKB
    from app.models.database import get_engine, get_session_maker
    from app.services.triage_engine import TriageEngine
    from app import main as main_module

    engine = await get_engine("sqlite+aiosqlite:///:memory:")
    session_maker = get_session_maker(engine)

    llm = MagicMock()
    llm.generate = AsyncMock(return_value=MagicMock(
        text='{"symptoms": [{"name": "головная боль", "area": "head"}], "needs_clarification": true}'
    ))
    triage = TriageEngine(llm=llm, kb=MedicalKB())

    main_module.db_session_maker = session_maker
    main_module.triage_engine = triage
    # Tests fire many requests rapidly; the production rate limit (20/min)
    # is not what we are testing here.
    app.state.limiter.enabled = False

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.state.limiter.enabled = True


async def _start_session(client) -> str:
    r = await client.post("/api/v1/session/start")
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


@pytest.mark.asyncio
async def test_crisis_locks_session_subsequent_messages_repeat_crisis(client_with_locked_llm):
    client = client_with_locked_llm
    sid = await _start_session(client)

    r1 = await client.post(f"/api/v1/session/{sid}/message", json={"text": "хочу умереть"})
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["is_emergency"] is True
    assert body1["session_status"] == "emergency"
    assert "8-800-2000-122" in body1["message"]

    # Neutral follow-up must NOT resume intake — it must repeat the crisis message.
    r2 = await client.post(f"/api/v1/session/{sid}/message", json={"text": "болит голова"})
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["is_emergency"] is True
    assert body2["session_status"] == "emergency"
    assert "8-800-2000-122" in body2["message"]


@pytest.mark.asyncio
async def test_compound_input_also_locks_session(client_with_locked_llm):
    """A crisis-priority match must lock the session, not just return the right message once."""
    client = client_with_locked_llm
    sid = await _start_session(client)

    r1 = await client.post(
        f"/api/v1/session/{sid}/message",
        json={"text": "хочу умереть от боли в груди"},
    )
    assert r1.status_code == 200
    assert r1.json()["is_emergency"] is True
    assert "8-800-2000-122" in r1.json()["message"]
    # The cardiac message must NOT have been picked
    assert "сердца" not in r1.json()["message"]

    r2 = await client.post(f"/api/v1/session/{sid}/message", json={"text": "хорошо"})
    assert r2.json()["is_emergency"] is True
    assert "8-800-2000-122" in r2.json()["message"]


@pytest.mark.asyncio
async def test_normal_red_flag_does_not_lock_session(client_with_locked_llm):
    """Physical red-flags must keep the session open — follow-up intake works."""
    client = client_with_locked_llm
    sid = await _start_session(client)

    r1 = await client.post(f"/api/v1/session/{sid}/message", json={"text": "сильная боль в груди"})
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["is_emergency"] is True
    # cardiac message, no hotline
    assert "8-800-2000-122" not in body1["message"]

    # Follow-up must reach the LLM extraction path, not bounce off a lock.
    r2 = await client.post(f"/api/v1/session/{sid}/message", json={"text": "что делать пока едет скорая"})
    assert r2.status_code == 200
    body2 = r2.json()
    # No crisis content
    assert "8-800-2000-122" not in body2["message"]


@pytest.mark.asyncio
async def test_crisis_loop_does_not_exhaust_message_limit(client_with_locked_llm):
    """35 follow-ups in a crisis loop must all return crisis, never HTTP 409 from MAX_SESSION_MESSAGES."""
    client = client_with_locked_llm
    sid = await _start_session(client)

    # Trigger crisis
    r = await client.post(f"/api/v1/session/{sid}/message", json={"text": "хочу умереть"})
    assert r.json()["is_emergency"] is True

    for i in range(35):
        rN = await client.post(f"/api/v1/session/{sid}/message", json={"text": f"follow-up #{i}"})
        assert rN.status_code == 200, f"request #{i} got {rN.status_code}: {rN.text}"
        assert rN.json()["is_emergency"] is True
        assert "8-800-2000-122" in rN.json()["message"]
