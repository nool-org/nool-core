import base64
import io


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
