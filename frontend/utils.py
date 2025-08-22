from pydub import AudioSegment
from pydub.playback import play
import io
import sounddevice as sd
import numpy as np
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)

def play_audio(audio_bytes, format_hint=None, sample_rate=16000):
    """
    Try to play audio bytes. Try MP3/WAV/WEBM via pydub first; if that fails, treat as raw PCM int16.
    """
    bio = io.BytesIO(audio_bytes)
    tried = []
    for fmt in (format_hint, "mp3", "wav", "webm", "ogg"):
        if not fmt:
            continue
        try:
            bio.seek(0)
            audio = AudioSegment.from_file(bio, format=fmt)
            play(audio)
            return
        except Exception as e:
            tried.append((fmt, str(e)))
    try:
        bio.seek(0)
        audio = AudioSegment.from_file(bio)
        play(audio)
        return
    except Exception:
        pass

    try:
        arr = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32767.0
        sd.play(arr, samplerate=sample_rate, blocking=False)
        return
    except Exception as e:
        print("play_audio failed. tried formats:", tried, "fallback err:", e)
