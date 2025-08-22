import os
from dotenv import load_dotenv

load_dotenv()
MURF_API_KEY = os.getenv("MURF_API_KEY")

client = None
try:
    from murf import Murf
    client = Murf(api_key=MURF_API_KEY)
except Exception:
    client = None

def generate_speech_from_text(text, language="en", voice="en-US-Wavenet-D"):
    """
    Generate MP3 bytes using Murf SDK. Adapt this call to the SDK you have.
    The wrapper returns raw bytes (MP3) so server can forward them immediately.
    """
    if client is None:
        raise RuntimeError("Murf client not initialized. Set MURF_API_KEY and install murf SDK.")

    response = client.text_to_speech.generate(
        text=text,
        voice_id=voice,
        format="MP3",
        sample_rate=44100.0,
        language=language
    )

    if hasattr(response, "audio_file") and isinstance(response.audio_file, str) and os.path.exists(response.audio_file):
        with open(response.audio_file, "rb") as f:
            return f.read()

    for attr in ("content", "audio", "audio_bytes", "data"):
        if hasattr(response, attr):
            blob = getattr(response, attr)
            if isinstance(blob, (bytes, bytearray)):
                return bytes(blob)

    if isinstance(response, (bytes, bytearray)):
        return bytes(response)

    raise RuntimeError("Unsupported Murf response shape; inspect the SDK response.")
