import json
import os
import io
import wave
import logging
import base64
import requests
from dotenv import load_dotenv
from murf import Murf
from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
import torch
from io import BytesIO
import numpy as np


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
logger.info("Initializing Murf client...")
client = Murf(api_key=MURF_API_KEY)
logger.info("Murf client initialized successfully")

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


MODEL_MAP = {
    "en-US": ("whisper", "openai/whisper-small"),
    "es-ES": ("whisper", "openai/whisper-small"),
    "fr-FR": ("whisper", "openai/whisper-small"),
    "de-DE": ("whisper", "openai/whisper-small"),
    "it-IT": ("whisper", "openai/whisper-small"),
    "nl-NL": ("whisper", "openai/whisper-small"),
    "pt-BR": ("whisper", "openai/whisper-small"),
    "zh-CN": ("whisper", "openai/whisper-small"),
    "ja-JP": ("whisper", "openai/whisper-small"),
    "ko-KR": ("whisper", "openai/whisper-small"),
    "hi-IN": ("whisper", "openai/whisper-small"),
    "pl-PL": ("whisper", "openai/whisper-small"),
    "el-GR": ("whisper", "openai/whisper-small"),
    "en-UK": ("whisper", "openai/whisper-small"),
    "en-IN": ("whisper", "openai/whisper-small"),
    "en-AU": ("whisper", "openai/whisper-small"),
    "en-SCOTT": ("whisper", "openai/whisper-small"),
    "es-MX": ("whisper", "openai/whisper-small"),

    "ta-IN": ("wav2vec2", "ai4bharat/indicwav2vec-tam"),
    "bn-IN": ("wav2vec2", "ai4bharat/indicwav2vec-ben"),
    "hr-HR": ("wav2vec2", "facebook/wav2vec2-large-xlsr-53-fine-tuned-hr"),  
    "sk-SK": ("wav2vec2", "facebook/wav2vec2-large-xlsr-53-fine-tuned-sk"),
}

HF_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
HF_API_URL = "https://api-inference.huggingface.co/models"


def resolve_language(user_choice: str, default="hi-IN") -> str:
    """Map user choice to STT-compatible language code."""
    return LANGUAGE_CODE_MAP.get(user_choice, default)


# -----------------------
# Helpers
# -----------------------
def normalize_language(code: str) -> str:
    """Convert human language string or shorthand to Murf locale."""
    if not code:
        logger.warning("No language code provided. Defaulting to hi-IN")
        return "hi-IN"
    if code in LANGUAGE_CODE_MAP:
        logger.debug("Normalized %s -> %s", code, LANGUAGE_CODE_MAP[code])
        return LANGUAGE_CODE_MAP[code]
    if "-" in code:
        logger.debug("Using provided language code: %s", code)
        return code
    logger.warning("Unsupported language code '%s'. Defaulting to hi-IN", code)
    return "hi-IN"


# -----------------------
# Voice helpers
# -----------------------
def get_available_voices(force_refresh: bool = False):
    """Fetch all voices from Murf API (cached by default)."""
    global _voice_cache
    if _voice_cache is None or force_refresh:
        logger.info("Fetching available voices from Murf API...")
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

    logger.debug("Searching for default voice for language=%s", language)

    # First try exact match
    for v in voices:
        locale = getattr(v, "locale", None)
        if locale and locale.startswith(language):
            logger.info("Found default voice %s for %s", v.voice_id, language)
            _default_voice_cache[language] = v.voice_id
            return v.voice_id

    # Fallback to Hindi
    for v in voices:
        locale = getattr(v, "locale", "")
        if locale.startswith("hi-IN"):
            logger.warning("No match found for %s. Falling back to Hindi voice=%s", language, v.voice_id)
            _default_voice_cache[language] = v.voice_id
            return v.voice_id

    logger.error("No valid voice found for %s and fallback failed", language)
    raise RuntimeError(f"No valid voice found for {language}")




# -----------------------
# Speech to Text (Mixed)
# -----------------------

def transcribe_with_hf(wav_bytes: bytes, model_id: str, language_code: str = None):
    """
    Call Hugging Face Inference API for ASR with optional language hint.
    """
    headers = {"Authorization": f"Bearer {HF_API_KEY}"}

    # Build payload: audio as binary, plus JSON params if language is provided
    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
    payload = {}
    if language_code:
        payload["parameters"] = {"language": language_code}

    response = requests.post(
        f"{HF_API_URL}/{model_id}",
        headers=headers,
        files=files,
        data={"json": json.dumps(payload)} if payload else None,
    )

    if response.status_code != 200:
        logger.error("HF API request failed [%d]: %s", response.status_code, response.text)
        raise RuntimeError(f"Hugging Face API error: {response.text}")

    try:
        result = response.json()
        if isinstance(result, dict) and "text" in result:
            return result["text"]
        if isinstance(result, list) and len(result) > 0 and "text" in result[0]:
            return result[0]["text"]
        logger.warning("HF API returned unexpected format: %s", result)
        return ""
    except Exception:
        logger.exception("Failed to parse Hugging Face response")
        raise


def speech_to_text(audio_bytes, sample_rate=16000, language="hi-IN"):
    logger.info("Starting STT with lang=%s, sample_rate=%s, bytes=%d",
                language, sample_rate, len(audio_bytes))
    
    language_code = resolve_language(language, default="hi-IN")
    logger.debug("Resolved language → %s", language_code)

    if language_code not in MODEL_MAP:
        raise ValueError(f"No model available for language {language_code}")
    
    backend, model_ref = MODEL_MAP[language_code]

    # --- Step 1: Ensure WAV format ---
    if len(audio_bytes) >= 4 and audio_bytes[:4] == b'RIFF':
        wav_bytes = audio_bytes
        logger.debug("Audio already in WAV format")
    else:
        logger.debug("Converting PCM → WAV")
        wav_bytes = _pcm_to_wav_bytes(
            audio_bytes, sample_rate=sample_rate, sample_width=2, channels=1
        )

    # --- Step 2: Use Hugging Face ---
    if backend == "whisper":
        # Whisper supports explicit language parameter
        hf_lang = language_code.split("-")[0]   # e.g. "hi-IN" → "hi"
        logger.debug("Using Whisper [%s] with language=%s", model_ref, hf_lang)
        return transcribe_with_hf(wav_bytes, model_ref, language_code=hf_lang)

    elif backend == "wav2vec2":
        # Wav2Vec2 is language-specific (fine-tuned), no extra parameter
        logger.debug("Using Wav2Vec2 [%s]", model_ref)
        return transcribe_with_hf(wav_bytes, model_ref)

    else:
        raise ValueError(f"Unknown backend {backend} for {language_code}")




def _pcm_to_wav_bytes(pcm_bytes, sample_rate=16000, sample_width=2, channels=1):
    """Wrap raw PCM16LE bytes in a WAV container."""
    with BytesIO() as wav_io:
        with wave.open(wav_io, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        return wav_io.getvalue()



# -----------------------
# Translation (Murf)
# -----------------------
def translate_text_murf(text, target_language="es-ES"):
    logger.info("Translating text to %s (input length=%d)", target_language, len(text))
    try:
        resp = client.text.translate(target_language=target_language, texts=[text])
        translations = [item.get("translated_text", "") for item in resp.get("translations", [])]
        translated = translations[0] if translations else ""
        logger.info("Translation complete. Output length=%d", len(translated))
        return translated
    except Exception:
        logger.exception("Translation failed")
        raise


# -----------------------
# Text to Speech (Murf)
# -----------------------
def generate_speech_from_text(text, language="en-US", voice=None):
    if not voice:
        voice = get_default_voice(language)

    logger.info("Starting TTS: lang=%s, voice=%s, text_length=%d", language, voice, len(text))
    response = None
    try:
        response = client.text_to_speech.generate(
            text=text,
            voice_id=voice,
            format="MP3",
            sample_rate=44100.0,
        )
        logger.info("TTS generation successful")
    except Exception:
        logger.exception("Murf TTS generate() failed")
        raise

    # Handle multiple response shapes
    if isinstance(response, (bytes, bytearray)):
        return bytes(response)

    if isinstance(response, dict) and "audio" in response:
        audio_obj = response["audio"]
        if isinstance(audio_obj, dict) and "data" in audio_obj:
            try:
                return base64.b64decode(audio_obj["data"])
            except Exception:
                logger.exception("Failed to base64 decode audio data")
                raise

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
                        logger.debug("Attribute %s not base64 decodable", attr)
                        continue

    if hasattr(response, "audio_file") and response.audio_file:
        try:
            r = requests.get(response.audio_file, timeout=10)
            r.raise_for_status()
            return r.content
        except Exception:
            logger.exception("Failed fetching audio from signed URL")
            raise

    logger.error("Unsupported Murf TTS response: %r", response)
    raise RuntimeError("Unsupported Murf TTS response shape")


# -----------------------
# Full pipeline: STT → Translate → TTS
# -----------------------
def process_audio_pipeline(audio_bytes, stt_lang="hi-IN", target_lang="es-ES", voice=None):
    logger.info("Pipeline start: stt_lang=%s, target_lang=%s, audio_bytes=%d", stt_lang, target_lang, len(audio_bytes))

    recognized = speech_to_text(audio_bytes, language=stt_lang)
    if not recognized:
        logger.warning("Pipeline: no speech recognized")
        return None, None, None

    translated = translate_text_murf(recognized, target_language=target_lang)
    if not translated:
        logger.warning("Pipeline: translation failed or empty")
        return recognized, None, None

    if not voice:
        voice = get_default_voice(target_lang)

    tts_bytes = generate_speech_from_text(translated, language=target_lang, voice=voice)
    logger.info("Pipeline completed successfully")
    return recognized, translated, tts_bytes
