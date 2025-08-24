import os
import io
import wave
from pydub import AudioSegment
from dotenv import load_dotenv
from murf import Murf

# Load API key
load_dotenv()
MURF_API_KEY = os.getenv("MURF_API_KEY")
if not MURF_API_KEY:
    raise RuntimeError("Please set MURF_API_KEY in your .env file or environment variables.")

# Init Murf client
client = Murf(api_key=MURF_API_KEY)

# -----------------------
# Audio Conversion Helpers
# -----------------------
def _pcm_to_wav_bytes(pcm_bytes, sample_rate=16000, sample_width=2, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    buf.seek(0)
    return buf.read()

def _try_convert_container_to_wav(blob_bytes):
    try:
        audio = AudioSegment.from_file(io.BytesIO(blob_bytes))
        out = io.BytesIO()
        audio.export(out, format="wav")
        out.seek(0)
        return out.read()
    except Exception:
        return None

# -----------------------
# Speech to Text (Murf)
# -----------------------
def speech_to_text_murf(audio_bytes, sample_rate=16000, language="en-US"):
    if len(audio_bytes) >= 4 and audio_bytes[:4] == b'RIFF':
        wav_bytes = audio_bytes
    else:
        wav_bytes = _try_convert_container_to_wav(audio_bytes)
        if wav_bytes is None:
            wav_bytes = _pcm_to_wav_bytes(audio_bytes, sample_rate=sample_rate, sample_width=2, channels=1)

    response = client.speech_to_text.transcribe(
        audio=wav_bytes,
        format="wav",
        language=language
    )
    return response.get("text", "")

# -----------------------
# Translation (Murf)
# -----------------------
def translate_text_murf(text, target_language="es-ES"):
    resp = client.text.translate(
        target_language=target_language,
        texts=[text]
    )
    translations = [item.get("translated_text", "") for item in resp.get("translations", [])]
    return translations[0] if translations else ""

# -----------------------
# Text to Speech (Murf)
# -----------------------
def generate_speech_from_text(text, language="en-US", voice="en-US-Wavenet-D"):
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

# -----------------------
# Full pipeline: STT → Translate → TTS
# -----------------------

def process_audio_pipeline(audio_bytes, stt_lang="en-US", target_lang="es-ES", voice="es-ES-Wavenet-A"):
    """
    1. Convert speech to text (stt_lang)
    2. Translate to target_lang
    3. Generate TTS in target_lang
    """
    recognized = speech_to_text_murf(audio_bytes, language=stt_lang)
    if not recognized:
        return None, None, None

    translated = translate_text_murf(recognized, target_language=target_lang)
    if not translated:
        return recognized, None, None

    tts_bytes = generate_speech_from_text(translated, language=target_lang, voice=voice)
    return recognized, translated, tts_bytes