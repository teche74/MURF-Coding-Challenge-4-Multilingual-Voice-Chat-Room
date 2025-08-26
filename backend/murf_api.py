import os
import io
import wave
import logging
from pydub import AudioSegment
from dotenv import load_dotenv
from murf import Murf

# -----------------------
# Logging setup
# -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("murf_pipeline")

# -----------------------
# Load API key
# -----------------------
load_dotenv()
MURF_API_KEY = os.getenv("MURF_API_KEY")
if not MURF_API_KEY:
    logger.error("MURF_API_KEY not found in environment")
    raise RuntimeError("Please set MURF_API_KEY in your .env file or environment variables.")

# Init Murf client
client = Murf(api_key=MURF_API_KEY)
logger.info("Murf client initialized")

# Cache for voices
_voice_cache = None
_default_voice_cache = {}

LANGUAGE_CODE_MAP = {
    "English - US & Canada": "en-US",
    "English - UK": "en-UK",
    "English - India": "en-IN",
    "English - Australia": "en-AU",
    "English - Scotland": "en-SCOTT",
    "Spanish - Mexico": "es-MX",
    "Spanish - Spain": "es-ES",
    "French - France": "fr-FR",
    "German - Germany": "de-DE",
    "Italian - Italy": "it-IT",
    "Dutch - Netherlands": "nl-NL",
    "Portuguese - Brazil": "pt-BR",
    "Chinese - China": "zh-CN",
    "Japanese - Japan": "ja-JP",
    "Korean - Korea": "ko-KR",
    "Hindi - India": "hi-IN",
    "Tamil - India": "ta-IN",
    "Bengali - India": "bn-IN",
    "Croatian - Croatia": "hr-HR",
    "Slovak - Slovakia": "sk-SK",
    "Polish - Poland": "pl-PL",
    "Greek - Greece": "el-GR"
}


def normalize_language(code: str) -> str:
    """Convert 2-letter code to Murf locale. Fallback to hi-IN."""
    if not code:
        return "hi-IN"
    if code in LANGUAGE_CODE_MAP:
        return LANGUAGE_CODE_MAP[code]
    if "-" in code:
        return code
    return "hi-IN"

# -----------------------
# Voice helpers
# -----------------------
def get_available_voices(force_refresh: bool = False):
    """Fetch all voices from Murf API (cached by default)."""
    global _voice_cache
    if _voice_cache is None or force_refresh:
        logger.info("Fetching available voices from Murf API...")
        _voice_cache = client.text_to_speech.get_voices()
    return _voice_cache


def get_default_voice(language: str) -> str:
    """Return a valid default voice_id for a given language (locale like 'hi-IN')."""
    if _default_voice_cache.get(language):
        return _default_voice_cache[language]

    voices = get_available_voices()

    # First try exact language match
    for v in voices:
        locale = getattr(v, "locale", None)
        if locale and locale.startswith(language):
            _default_voice_cache[language] = v.voice_id
            return v.voice_id

    # Fallback to Hindi (hi-IN) if nothing found
    for v in voices:
        locale = getattr(v, "locale", "")
        if locale.startswith("hi-IN"):
            _default_voice_cache[language] = v.voice_id
            return v.voice_id

    raise RuntimeError(f"No valid voice found for {language} and fallback failed")
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
    except Exception as e:
        logger.warning("Container conversion failed: %s", e)
        return None

# -----------------------
# Speech to Text (Murf)
# -----------------------
def speech_to_text_murf(audio_bytes, sample_rate=16000, language="hi-IN"):
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
    logger.info("STT: received %d bytes, sample_rate=%s, language=%s", len(audio_bytes), sample_rate, language)
    return response.get("text", "")

# -----------------------
# Translation (Murf)
# -----------------------
def translate_text_murf(text, target_language="es-ES"):
    resp = client.text.translate(target_language=target_language, texts=[text])
    translations = [item.get("translated_text", "") for item in resp.get("translations", [])]
    return translations[0] if translations else ""

# -----------------------
# Text to Speech (Murf)
# -----------------------
import base64
import logging
import requests

logger = logging.getLogger("murf_pipeline")

def generate_speech_from_text(text, language="en-US", voice=None):
    if not voice:
        voice = get_default_voice(language)

    logger.info("TTS start: lang=%s, voice=%s", language, voice)
    response = None
    try:
        response = client.text_to_speech.generate(
            text=text,
            voice_id=voice,
            format="MP3",
            sample_rate=44100.0,
        )
    except Exception:
        logger.exception("Murf TTS generate() failed")
        raise

    # bytes directly
    if isinstance(response, (bytes, bytearray)):
        return bytes(response)

    # dict with base64
    if isinstance(response, dict) and "audio" in response:
        audio_obj = response["audio"]
        if isinstance(audio_obj, dict) and "data" in audio_obj:
            return base64.b64decode(audio_obj["data"])

    # SDK object variants
    for attr in ("content", "audio", "audio_bytes", "data", "encoded_audio"):
        if hasattr(response, attr):
            blob = getattr(response, attr)
            if blob:
                if isinstance(blob, (bytes, bytearray)):
                    return bytes(blob)
                if isinstance(blob, str):
                    try:
                        return base64.b64decode(blob)
                    except Exception:
                        pass

    if hasattr(response, "audio_file") and response.audio_file:
        r = requests.get(response.audio_file, timeout=10)
        r.raise_for_status()
        return r.content

    logger.error("Unsupported Murf TTS response: %r", response)
    raise RuntimeError("Unsupported Murf TTS response shape")


# -----------------------
# Full pipeline: STT → Translate → TTS
# -----------------------
def process_audio_pipeline(audio_bytes, stt_lang="hi-IN", target_lang="es-ES", voice=None):
    recognized = speech_to_text_murf(audio_bytes, language=stt_lang)
    if not recognized:
        return None, None, None

    translated = translate_text_murf(recognized, target_language=target_lang)
    if not translated:
        return recognized, None, None

    if not voice:
        voice = get_default_voice(target_lang)

    tts_bytes = generate_speech_from_text(translated, language=target_lang, voice=voice)
    return recognized, translated, tts_bytes
