import httpx

from app.core.errors import AppError


MIME_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp4",
    "audio/flac",
    "video/webm",
}


class GroqTranscriber:
    def __init__(self, settings):
        self.settings = settings

    async def transcribe(self, data: bytes, content_type: str, duration_seconds: float | None = None):
        if not self.settings.groq_api_key:
            raise AppError(
                "voice_not_configured",
                "Voice transcription is unavailable because GROQ_API_KEY is not configured.",
                503,
            )
        mime = (content_type or "").split(";")[0].lower()
        if mime not in MIME_TYPES:
            raise AppError(
                "voice_invalid_type", "Use a WebM, Ogg, WAV, MP3, MP4, or FLAC audio recording.", 415
            )
        if not data or len(data) > self.settings.voice_max_upload_mb * 1024 * 1024:
            raise AppError("voice_invalid_size", "Recording is empty or exceeds the upload limit.", 413)
        if duration_seconds is not None and duration_seconds > self.settings.voice_max_duration_seconds:
            raise AppError("voice_duration_exceeded", "Recording exceeds the configured duration limit.", 413)
        suffix = {
            "audio/webm": "webm",
            "video/webm": "webm",
            "audio/ogg": "ogg",
            "audio/mp4": "mp4",
            "audio/mpeg": "mp3",
            "audio/flac": "flac",
        }.get(mime, "wav")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.settings.groq_api_key}"},
                    files={"file": (f"recording.{suffix}", data, mime)},
                    data={"model": self.settings.groq_whisper_model, "response_format": "verbose_json"},
                )
            if response.status_code >= 400:
                raise AppError(
                    "voice_provider_unavailable",
                    "Voice provider rejected the recording. Try again.",
                    503,
                    retryable=response.status_code == 429 or response.status_code >= 500,
                )
            payload = response.json()
            if float(payload.get("duration", 0)) > self.settings.voice_max_duration_seconds:
                raise AppError(
                    "voice_duration_exceeded", "Recording exceeds the configured duration limit.", 413
                )
            text = str(payload.get("text", "")).strip()
            if not text:
                raise AppError("voice_empty_transcript", "No speech was detected. Try recording again.", 422)
            return {"text": text}
        except httpx.HTTPError:
            raise AppError(
                "voice_provider_unavailable",
                "Voice transcription is temporarily unavailable.",
                503,
                retryable=True,
            ) from None
