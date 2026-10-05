import base64
import io
import re

import edge_tts


def sanitize_text_for_speech(text: str) -> str:
    """Converts mathematical equations, LaTeX, and symbols into natural spoken English phonetics."""
    # Strip prompt tags
    text = re.sub(r'\[CO-LEARNER\]:', '', text)
    text = re.sub(r'\[SESSION_COMPLETED\]', '', text)

    # Common Physics formulas phonetics
    text = re.sub(r'\$?[Rr]\s*=\s*(?:\\rho|ρ)\s*\*?\s*[Ll]\s*/\s*[Aa]\$?', 'resistance equals resistivity times length divided by area', text)
    text = re.sub(r'\$?[Vv]\s*=\s*[Ii]\s*\*?\s*[Rr]\$?', 'V equals I times R', text)
    text = re.sub(r'\$?[Hh]\s*=\s*[Ii]\^?2\s*\*?\s*[Rr]\s*\*?\s*[Tt]\$?', 'heat equals I squared times R times t', text)
    text = re.sub(r'\$?[Ii]\s*=\s*[Qq]\s*/\s*[Tt]\$?', 'current equals charge divided by time', text)
    text = re.sub(r'\$?[Pp]\s*=\s*[Vv]\^?2\s*/\s*[Rr]\$?', 'power equals V squared divided by R', text)

    # Symbols & math replacements
    replacements = [
        (r'\\rho|ρ', 'resistivity'),
        (r'\\Omega|Ω', ' ohms'),
        (r'\\mu|μ', 'micro'),
        (r'\\Delta|Δ', 'delta'),
        (r'\^2', ' squared'),
        (r'\^3', ' cubed'),
        (r'\$', ''),
        (r'\\times|\\cdot|\*', ' times '),
        (r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1 over \2'),
        (r'\\', ''),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)

    return re.sub(r'\s+', ' ', text).strip()


async def synthesize_speech_base64(text: str, voice: str, rate: str) -> str:
    """Synthesizes text using Edge-TTS with formula phonetics and returns base64 MP3."""
    clean_text = sanitize_text_for_speech(text)
    if not clean_text:
        return ""
    try:
        communicate = edge_tts.Communicate(clean_text, voice, rate=rate)
        audio_stream = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_stream.write(chunk["data"])
        audio_stream.seek(0)
        return base64.b64encode(audio_stream.getvalue()).decode("utf-8")
    except Exception as ex:
        print(f"Edge-TTS Synthesis Error: {ex}")
        return ""


def decode_base64_audio(b64_string: str) -> bytes:
    """Decodes base64-encoded audio bytes, stripping data URI header if present."""
    if not b64_string:
        return b""
    if "," in b64_string:
        b64_string = b64_string.split(",", 1)[1]
    return base64.b64decode(b64_string)


# (extension, mime type) for each recognized container, keyed by a
# detection function over the leading bytes. Real recorded audio arrives
# in whatever container the caller's platform produces - nool-apps'
# student mic capture (expo-audio's HIGH_QUALITY preset) sends M4A/MP4,
# not WAV/WebM, so a filename/mime hint for an STT call must recognize it
# correctly rather than defaulting to "webm" for everything non-RIFF - a
# wrong guess can silently produce garbage/empty transcription, not an
# error. No production pipeline calls this today (realtime streams audio
# over WebRTC; cascaded converts to WAV first); kept with its tests.
def detect_audio_container(audio_bytes: bytes) -> tuple[str, str]:
    """Returns (file_extension_with_dot, mime_type) detected from the
    leading bytes. Falls back to WebM (the one non-detectable format this
    app's own browser-based recording, if any, would produce).
    """
    if audio_bytes[:4] == b"RIFF":
        return ".wav", "audio/wav"
    if audio_bytes[4:8] == b"ftyp":
        return ".m4a", "audio/mp4"
    if audio_bytes[:4] == b"OggS":
        return ".ogg", "audio/ogg"
    return ".webm", "audio/webm"


def convert_audio_to_wav(audio_bytes: bytes, target_rate: int = 24000) -> bytes:
    """Converts any audio byte stream (WebM, OGG, MP3, AAC) to standard 16-bit PCM WAV in memory."""
    if not audio_bytes:
        return b""
    if audio_bytes[:4] == b"RIFF":
        return audio_bytes
    try:
        import av
        input_io = io.BytesIO(audio_bytes)
        output_io = io.BytesIO()
        with av.open(input_io) as in_container, av.open(output_io, "w", format="wav") as out_container:
            if not in_container.streams.audio:
                return audio_bytes
            in_stream = in_container.streams.audio[0]
            out_stream = out_container.add_stream("pcm_s16le", rate=target_rate)
            out_stream.layout = "mono"
            for frame in in_container.decode(in_stream):
                frame.pts = None
                for packet in out_stream.encode(frame):
                    out_container.mux(packet)
            for packet in out_stream.encode(None):
                out_container.mux(packet)
        return output_io.getvalue()
    except Exception as e:
        print(f"Audio to WAV conversion warning: {e}")
        return audio_bytes
