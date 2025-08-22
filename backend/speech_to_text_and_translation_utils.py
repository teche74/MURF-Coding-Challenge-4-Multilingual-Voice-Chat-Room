import io
import wave
import speech_recognition as sr
from googletrans import Translator
from pydub import AudioSegment

translator = Translator()

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
    """
    Try to decode container formats (webm/ogg/mp3/wav) using pydub and return WAV bytes.
    """
    try:
        audio = AudioSegment.from_file(io.BytesIO(blob_bytes))
        out = io.BytesIO()
        audio.export(out, format="wav")
        out.seek(0)
        return out.read()
    except Exception:
        return None

def speech_to_text(audio_bytes, sample_rate=16000):
    """
    Accepts:
     - WAV bytes (RIFF header)
     - webm/opus/mp3 container bytes (MediaRecorder)
     - raw PCM int16 bytes (no header)
    Returns recognized text using speech_recognition (Google web recognizer).
    """
    if len(audio_bytes) >= 4 and audio_bytes[:4] == b'RIFF':
        wav_bytes = audio_bytes
    else:
        wav_bytes = _try_convert_container_to_wav(audio_bytes)
        if wav_bytes is None:
            wav_bytes = _pcm_to_wav_bytes(audio_bytes, sample_rate=sample_rate, sample_width=2, channels=1)

    r = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(wav_bytes)) as source:
        audio = r.record(source)
    try:
        return r.recognize_google(audio)
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        raise RuntimeError(f"STT request failed: {e}")

def translate_text(text, target_lang="en"):
    translated = translator.translate(text, dest=target_lang)
    return translated.text
