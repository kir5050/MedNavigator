"""Tests for POST /api/v1/transcribe (voice input, feature-flagged).

OpenRouter is never called: endpoint tests mock app.services.stt, and
service-level tests use httpx.MockTransport.
"""

import base64
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as main_module
from app.config import settings
from app.services import stt

WEBM_FILE = ("rec.webm", b"\x1aE\xdf\xa3voice-bytes", "audio/webm")


def _client() -> AsyncClient:
    transport = ASGITransport(app=main_module.app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def voice_on(monkeypatch):
    monkeypatch.setattr(settings, "voice_input_enabled", True)
    main_module.app.state.limiter.enabled = False
    yield
    main_module.app.state.limiter.enabled = True


@pytest.fixture
def voice_off(monkeypatch):
    monkeypatch.setattr(settings, "voice_input_enabled", False)
    main_module.app.state.limiter.enabled = False
    yield
    main_module.app.state.limiter.enabled = True


@pytest.fixture
def stt_ok(monkeypatch):
    mock = AsyncMock(return_value=("Болит голова и тошнит со вчерашнего дня", 1234))
    monkeypatch.setattr(stt, "transcribe_audio", mock)
    return mock


class TestFeatureFlag:
    @pytest.mark.asyncio
    async def test_flag_off_returns_404(self, voice_off, stt_ok):
        async with _client() as client:
            resp = await client.post("/api/v1/transcribe", files={"audio": WEBM_FILE})
        assert resp.status_code == 404
        stt_ok.assert_not_called()

    @pytest.mark.asyncio
    async def test_flag_off_is_indistinguishable_from_missing_route(
        self, voice_off, stt_ok
    ):
        async with _client() as client:
            disabled = await client.post("/api/v1/transcribe", files={"audio": WEBM_FILE})
            missing = await client.post("/api/v1/no-such-route", files={"audio": WEBM_FILE})
        assert disabled.status_code == missing.status_code == 404
        assert disabled.json() == missing.json()

    @pytest.mark.asyncio
    async def test_flag_off_flood_never_returns_429(self, voice_off, stt_ok):
        """Rate limiter must be exempt while the flag is off: a 429 would
        reveal that the route exists."""
        limiter = main_module.app.state.limiter
        limiter.reset()
        limiter.enabled = True
        try:
            async with _client() as client:
                statuses = {
                    (
                        await client.post(
                            "/api/v1/transcribe", files={"audio": WEBM_FILE}
                        )
                    ).status_code
                    for _ in range(12)
                }
        finally:
            limiter.enabled = False
            limiter.reset()
        assert statuses == {404}


class TestValidation:
    @pytest.mark.asyncio
    async def test_unsupported_content_type_415(self, voice_on, stt_ok):
        async with _client() as client:
            resp = await client.post(
                "/api/v1/transcribe",
                files={"audio": ("note.txt", b"plain text", "text/plain")},
            )
        assert resp.status_code == 415
        stt_ok.assert_not_called()

    @pytest.mark.asyncio
    async def test_oversize_audio_413(self, voice_on, stt_ok):
        big = b"\0" * (main_module.VOICE_MAX_AUDIO_BYTES + 1)
        async with _client() as client:
            resp = await client.post(
                "/api/v1/transcribe", files={"audio": ("rec.webm", big, "audio/webm")}
            )
        assert resp.status_code == 413
        stt_ok.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_audio_field_422(self, voice_on, stt_ok):
        async with _client() as client:
            resp = await client.post(
                "/api/v1/transcribe",
                files={"sound": ("rec.webm", b"bytes", "audio/webm")},
            )
        assert resp.status_code == 422
        stt_ok.assert_not_called()


class TestTranscription:
    @pytest.mark.asyncio
    async def test_success_returns_text_and_duration(self, voice_on, stt_ok):
        async with _client() as client:
            resp = await client.post("/api/v1/transcribe", files={"audio": WEBM_FILE})
        assert resp.status_code == 200
        assert resp.json() == {
            "text": "Болит голова и тошнит со вчерашнего дня",
            "duration_ms": 1234,
        }
        passed_bytes, passed_format, _api_key = stt_ok.call_args[0]
        assert passed_bytes == WEBM_FILE[1]
        assert passed_format == "webm"

    @pytest.mark.asyncio
    async def test_codecs_suffix_in_content_type_is_accepted(self, voice_on, stt_ok):
        """MediaRecorder blobs carry e.g. audio/webm;codecs=opus."""
        async with _client() as client:
            resp = await client.post(
                "/api/v1/transcribe",
                files={"audio": ("rec.webm", b"opus-bytes", "audio/webm;codecs=opus")},
            )
        assert resp.status_code == 200
        assert stt_ok.call_args[0][1] == "webm"

    @pytest.mark.asyncio
    async def test_mp4_upload_maps_to_m4a_format(self, voice_on, stt_ok):
        """iOS Safari records audio/mp4; OpenRouter expects format "m4a"."""
        async with _client() as client:
            resp = await client.post(
                "/api/v1/transcribe",
                files={"audio": ("rec.mp4", b"aac-bytes", "audio/mp4")},
            )
        assert resp.status_code == 200
        assert stt_ok.call_args[0][1] == "m4a"

    @pytest.mark.asyncio
    async def test_binary_payload_survives_multipart_parse(self, voice_on, stt_ok):
        """The in-memory multipart parser must round-trip arbitrary bytes,
        including CRLFs, NULs and boundary-looking lines."""
        tricky = b"\r\n--fake-boundary\r\n\x00\xff\xfe" + bytes(range(256)) + b"\r\n"
        async with _client() as client:
            resp = await client.post(
                "/api/v1/transcribe",
                files={"audio": ("rec.ogg", tricky, "audio/ogg")},
            )
        assert resp.status_code == 200
        assert stt_ok.call_args[0][0] == tricky
        assert stt_ok.call_args[0][1] == "ogg"

    @pytest.mark.asyncio
    async def test_stt_failure_returns_502(self, voice_on, monkeypatch):
        monkeypatch.setattr(
            stt,
            "transcribe_audio",
            AsyncMock(side_effect=stt.TranscriptionError("boom")),
        )
        async with _client() as client:
            resp = await client.post("/api/v1/transcribe", files={"audio": WEBM_FILE})
        assert resp.status_code == 502
        assert resp.json() == {"error": "stt_failed"}

    @pytest.mark.asyncio
    async def test_short_transcript_returns_422(self, voice_on, monkeypatch):
        monkeypatch.setattr(
            stt, "transcribe_audio", AsyncMock(return_value=("да", 800))
        )
        async with _client() as client:
            resp = await client.post("/api/v1/transcribe", files={"audio": WEBM_FILE})
        assert resp.status_code == 422
        assert resp.json() == {"error": "empty_transcript"}

    @pytest.mark.asyncio
    async def test_empty_transcript_returns_422(self, voice_on, monkeypatch):
        monkeypatch.setattr(
            stt, "transcribe_audio", AsyncMock(return_value=("", 800))
        )
        async with _client() as client:
            resp = await client.post("/api/v1/transcribe", files={"audio": WEBM_FILE})
        assert resp.status_code == 422
        assert resp.json() == {"error": "empty_transcript"}


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_eleventh_request_within_window_is_429(self, voice_on, stt_ok):
        limiter = main_module.app.state.limiter
        limiter.reset()
        limiter.enabled = True
        try:
            async with _client() as client:
                statuses = [
                    (
                        await client.post(
                            "/api/v1/transcribe", files={"audio": WEBM_FILE}
                        )
                    ).status_code
                    for _ in range(11)
                ]
        finally:
            limiter.enabled = False
            limiter.reset()
        assert statuses[:10] == [200] * 10
        assert statuses[10] == 429


class TestLogPrivacy:
    AUDIO_MARKER = b"AUDIO-BYTES-MARKER-7f3a" * 4
    TEXT_MARKER = "СЕКРЕТНАЯ ФРАЗА ПАЦИЕНТА МАРКЕР болит бок справа"

    @pytest.mark.asyncio
    async def test_endpoint_logs_contain_no_audio_or_transcript(
        self, voice_on, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            stt, "transcribe_audio", AsyncMock(return_value=(self.TEXT_MARKER, 50))
        )
        with caplog.at_level(logging.DEBUG):
            async with _client() as client:
                resp = await client.post(
                    "/api/v1/transcribe",
                    files={"audio": ("rec.webm", self.AUDIO_MARKER, "audio/webm")},
                )
        assert resp.status_code == 200
        b64 = base64.b64encode(self.AUDIO_MARKER).decode("ascii")
        assert "AUDIO-BYTES-MARKER" not in caplog.text
        assert b64 not in caplog.text
        assert "МАРКЕР" not in caplog.text
        assert self.TEXT_MARKER not in caplog.text

    @pytest.mark.asyncio
    async def test_service_logs_contain_no_audio_or_transcript(self, caplog):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"text": self.TEXT_MARKER})

        with caplog.at_level(logging.DEBUG):
            text, _ = await stt.transcribe_audio(
                self.AUDIO_MARKER,
                "webm",
                "test-key",
                transport=httpx.MockTransport(handler),
            )
        assert text == self.TEXT_MARKER
        b64 = base64.b64encode(self.AUDIO_MARKER).decode("ascii")
        assert "AUDIO-BYTES-MARKER" not in caplog.text
        assert b64 not in caplog.text
        assert "МАРКЕР" not in caplog.text
        assert "test-key" not in caplog.text

    @pytest.mark.asyncio
    async def test_service_failure_logs_contain_no_audio(self, caplog):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow upstream")

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(stt.TranscriptionError):
                await stt.transcribe_audio(
                    self.AUDIO_MARKER,
                    "webm",
                    "test-key",
                    transport=httpx.MockTransport(handler),
                )
        b64 = base64.b64encode(self.AUDIO_MARKER).decode("ascii")
        assert "AUDIO-BYTES-MARKER" not in caplog.text
        assert b64 not in caplog.text


class TestSttService:
    @pytest.mark.asyncio
    async def test_request_shape_and_response_parsing(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200, json={"text": "  Болит горло третий день  ", "usage": {}}
            )

        text, duration_ms = await stt.transcribe_audio(
            b"raw-audio",
            "m4a",
            "test-key",
            transport=httpx.MockTransport(handler),
        )

        assert captured["url"] == stt.OPENROUTER_TRANSCRIPTION_URL
        assert captured["auth"] == "Bearer test-key"
        assert captured["body"]["model"] == stt.STT_MODEL
        assert captured["body"]["language"] == "ru"
        assert captured["body"]["input_audio"]["format"] == "m4a"
        assert captured["body"]["input_audio"]["data"] == base64.b64encode(
            b"raw-audio"
        ).decode("ascii")
        # Trimmed text, integer duration
        assert text == "Болит горло третий день"
        assert isinstance(duration_ms, int) and duration_ms >= 0

    @pytest.mark.asyncio
    async def test_upstream_error_status_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "upstream"}})

        with pytest.raises(stt.TranscriptionError):
            await stt.transcribe_audio(
                b"raw", "webm", "k", transport=httpx.MockTransport(handler)
            )

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("upstream took too long")

        with pytest.raises(stt.TranscriptionError):
            await stt.transcribe_audio(
                b"raw", "webm", "k", transport=httpx.MockTransport(handler)
            )

    @pytest.mark.asyncio
    async def test_non_json_response_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        with pytest.raises(stt.TranscriptionError):
            await stt.transcribe_audio(
                b"raw", "webm", "k", transport=httpx.MockTransport(handler)
            )
