"""Speech-to-text via OpenRouter's audio transcription API (voice input).

Kept separate from the chat-completion LLM layer on purpose: STT has no
provider chain, no caching and no retries (a single call per user action),
and its privacy rules are stricter — audio bytes live in memory only, and
neither audio content nor transcript content is ever logged.
"""

import base64
import logging
import time

import httpx

logger = logging.getLogger(__name__)

OPENROUTER_TRANSCRIPTION_URL = "https://openrouter.ai/api/v1/audio/transcriptions"

# Verified against the OpenRouter catalog on 2026-06-10
# (GET /api/v1/models?output_modalities=transcription).
STT_MODEL = "openai/gpt-4o-transcribe"

STT_TIMEOUT_SECONDS = 60.0


class TranscriptionError(Exception):
    """STT call failed: network error, timeout or unusable response."""


async def transcribe_audio(
    audio_bytes: bytes,
    audio_format: str,
    api_key: str,
    model: str = STT_MODEL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, int]:
    """Transcribe in-memory audio bytes; return (trimmed text, duration_ms).

    OpenRouter's transcription endpoint is JSON-only (base64-encoded audio
    in `input_audio`), not OpenAI-style multipart. The base64 copy exists
    in memory for the duration of the request only. No retries by design.
    `transport` is injectable for tests only.
    """
    payload = {
        "model": model,
        "input_audio": {
            "data": base64.b64encode(audio_bytes).decode("ascii"),
            "format": audio_format,
        },
        "language": "ru",
    }

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=STT_TIMEOUT_SECONDS, transport=transport
        ) as client:
            resp = await client.post(
                OPENROUTER_TRANSCRIPTION_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        # Metadata only — never log audio bytes or transcript content.
        logger.warning(
            "STT request failed: error=%s model=%s duration_ms=%d",
            type(exc).__name__,
            model,
            duration_ms,
        )
        raise TranscriptionError("stt request failed") from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    # Metadata only — never log audio bytes or transcript content.
    logger.info(
        "STT response: model=%s status=%d duration_ms=%d",
        model,
        resp.status_code,
        duration_ms,
    )

    if resp.status_code != 200:
        # Surface the upstream error reason for operations. This is
        # OpenRouter's own diagnostic text from a FAILED request — there is
        # no transcript and no audio in it, so logging it keeps the
        # metadata-only rule intact.
        upstream_message = ""
        try:
            err = resp.json().get("error")
            if isinstance(err, dict):
                upstream_message = str(err.get("message", ""))[:200]
        except (ValueError, AttributeError):
            pass
        logger.warning(
            "STT upstream error: model=%s status=%d message=%s",
            model,
            resp.status_code,
            upstream_message or "<none>",
        )
        raise TranscriptionError(f"stt status {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise TranscriptionError("stt response is not json") from exc
    if not isinstance(data, dict):
        raise TranscriptionError("stt response is not an object")
    text = data.get("text", "")
    if not isinstance(text, str):
        raise TranscriptionError("stt response text is not a string")

    return text.strip(), duration_ms
