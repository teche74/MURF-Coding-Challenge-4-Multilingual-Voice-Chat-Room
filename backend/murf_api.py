import os
import logging
import base64
import requests
from dotenv import load_dotenv
from murf import Murf

logger = logging.getLogger("murf_pipeline")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

load_dotenv()

MURF_API_KEY = os.getenv("MURF_API_KEY")
if not MURF_API_KEY:
    logger.error("MURF_API_KEY not found in environment")
    raise RuntimeError("Please set MURF_API_KEY in your .env file or environment variables.")

logger.info("Initializing Murf client...")
client = Murf(api_key=MURF_API_KEY)
logger.info("Murf client initialized successfully")

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


def resolve_language(user_choice: str, default="hi-IN") -> str:
    """Map user choice to STT/TTS-compatible language code."""
    return LANGUAGE_CODE_MAP.get(user_choice, default)


def normalize_language(code: str) -> str:
    """Convert human language string or shorthand to Murf locale."""
    if not code:
        logger.warning("No language code provided. Defaulting to hi-IN")
        return "hi-IN"
    if code in LANGUAGE_CODE_MAP:
        return LANGUAGE_CODE_MAP[code]
    if "-" in code:
        return code
    logger.warning("Unsupported language code '%s'. Defaulting to hi-IN", code)
    return "hi-IN"


def get_available_voices(force_refresh: bool = False):
    """Fetch all voices from Murf API (cached by default)."""
    global _voice_cache
    if _voice_cache is None or force_refresh:
        try:
            _voice_cache = client.text_to_speech.get_voices()
            logger.info("Retrieved %d voices from Murf API", len(_voice_cache))
        except Exception:
            logger.exception("Failed to fetch voices from Murf API")
            raise
    return _voice_cache


def get_default_voice(language: str) -> str:
    """Return a valid default voice_id for a given language."""
    if _default_voice_cache.get(language):
        return _default_voice_cache[language]

    voices = get_available_voices()

    for v in voices:
        locale = getattr(v, "locale", None)
        if locale and locale.startswith(language):
            _default_voice_cache[language] = v.voice_id
            return v.voice_id

    for v in voices:
        locale = getattr(v, "locale", "")
        if locale.startswith("hi-IN"):
            logger.warning("No match found for %s. Falling back to Hindi voice=%s", language, v.voice_id)
            _default_voice_cache[language] = v.voice_id
            return v.voice_id

    logger.error("No valid voice found for %s and fallback failed", language)
    raise RuntimeError(f"No valid voice found for {language}")


def translate_text_murf(text, target_language="hi-IN"):
    """Translate text via Murf API."""
    logger.info("Translating text to %s (input length=%d)", target_language, len(text))
    try:
        resp = client.text.translate(target_language=target_language, texts=[text])
        translations = [item.get("translated_text", "") for item in resp.get("translations", [])]
        return translations[0] if translations else ""
    except Exception:
        logger.exception("Translation failed")
        raise


def generate_speech_from_text(text, language="hi-IN", voice=None):
    """Generate TTS audio bytes via Murf API."""
    if not voice:
        voice = get_default_voice(language)

    logger.info("Starting TTS: lang=%s, voice=%s, text_length=%d", language, voice, len(text))
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

    if isinstance(response, (bytes, bytearray)):
        return bytes(response)

    if isinstance(response, dict) and "audio" in response:
        audio_obj = response["audio"]
        if isinstance(audio_obj, dict) and "data" in audio_obj:
            return base64.b64decode(audio_obj["data"])

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
                        continue

    if hasattr(response, "audio_file") and response.audio_file:
        r = requests.get(response.audio_file, timeout=10)
        r.raise_for_status()
        return r.content

    logger.error("Unsupported Murf TTS response: %r", response)
    raise RuntimeError("Unsupported Murf TTS response shape")
