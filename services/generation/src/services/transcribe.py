"""Speech-to-text for the Paper Copilot's voice messages (gpt-4o-mini-transcribe,
English, prompted with the paper's subject/chapter words so terms like
"refraction" or "Ohm's law" come out right)."""

import base64
import binascii

from openai import AsyncOpenAI

from shared import llm_gateway
from pydantic import BaseModel, Field

from src.config.settings import get_settings
from src.services.cost import log_transcription_usage

MODEL = "gpt-4o-mini-transcribe"
MAX_AUDIO_BYTES = 5 * 1024 * 1024
# file extension the API infers the format from; Expo records m4a by default
_EXTENSIONS = {"audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/mp4": "m4a", "audio/aac": "m4a",
               "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
               "audio/webm": "webm", "audio/ogg": "ogg"}


class TranscribeRequest(BaseModel):
    audio_base64: str = Field(min_length=1, max_length=(MAX_AUDIO_BYTES * 4) // 3 + 4)
    mime_type: str = Field(default="audio/m4a", max_length=50)
    prompt: str = Field(default="", max_length=1000)
    # "en" for the Copilot; "auto" lets a student's mixed Tamil/Hindi-English answer through unforced
    language: str = Field(default="en", pattern=r"^(auto|[a-z]{2})$")

    def audio(self) -> bytes:
        try:
            data = base64.b64decode(self.audio_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("audioBase64 is not valid base64") from exc
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise ValueError("audio must be 1 byte to 5 MB")
        return data

    def filename(self) -> str:
        return f"audio.{_EXTENSIONS.get(self.mime_type.lower(), 'm4a')}"


class TranscribeResult(BaseModel):
    text: str


class OpenAITranscriber:
    def __init__(self, api_key: str, timeout: float = 60.0):
        self.client = AsyncOpenAI(**llm_gateway.client_kwargs(api_key), timeout=timeout, max_retries=0)

    async def __call__(self, *, audio: bytes, filename: str, prompt: str, language: str = "en") -> str:
        extra = {} if language == "auto" else {"language": language}
        result = await self.client.audio.transcriptions.create(
            model=MODEL, file=(filename, audio), prompt=prompt or None, **extra,
            **llm_gateway.call_kwargs("audio"),
        )
        log_transcription_usage(MODEL, getattr(result, "usage", None))
        return result.text


def get_transcriber() -> OpenAITranscriber | None:
    """FastAPI dependency - None (-> 503) without a key; tests override it."""
    key = get_settings().openai_api_key
    return OpenAITranscriber(key) if key else None
