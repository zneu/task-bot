import os
import re
from groq import Groq

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = groq_client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3",
        response_format="text",
    )
    return clean_transcription(transcription.strip())


def clean_transcription(text: str) -> str:
    """Post-process Whisper transcription for command parsing.

    - Strip trailing punctuation on short utterances (likely commands)
    - Preserve casing for natural "I heard:" display
    """
    # Short utterances (< 60 chars) are likely commands — strip trailing punctuation
    if len(text) < 60:
        text = re.sub(r'[.!?,;:]+$', '', text)
    return text
