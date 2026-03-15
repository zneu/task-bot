import os
from groq import Groq

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = groq_client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3",
        response_format="text",
    )
    return transcription.strip()
