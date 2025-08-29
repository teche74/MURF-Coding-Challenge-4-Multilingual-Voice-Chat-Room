import asyncio
import io
import logging
import os
from typing import Dict, Optional, List

from dotenv import load_dotenv

from livekit.agents.voice import Agent, AgentSession
from livekit import rtc
from livekit.api import AccessToken, VideoGrants
from livekit.rtc.audio_frame import AudioFrame
import numpy as np
from pydub import AudioSegment

from livekit.plugins import openai 

from backend.murf_api import (
    translate_text_murf,
    generate_speech_from_text,
    get_default_voice,
)

logger = logging.getLogger("bot")

if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s - %(message)s')
    ch.setFormatter(fmt)
    logger.addHandler(ch)
logger.setLevel(logging.DEBUG)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(os.path.dirname(BASE_DIR), ".env")
load_dotenv(ENV)

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")

MAX_CONCURRENT_TTS = 3
FRAME_MS = 20


async def _bytes_to_audio_frames_async(audio_bytes: bytes, sample_rate: int = 44100, channels: int = 1, frame_ms: int = FRAME_MS):
    try:
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
        audio = audio.set_frame_rate(sample_rate).set_channels(channels).set_sample_width(2)
        raw = audio.raw_data
    except Exception as e:
        logger.exception("[tts.frames] failed to decode/normalize TTS bytes")
        raise

    bytes_per_sample = 2
    samples_per_ms = sample_rate / 1000.0
    samples_per_chunk = int(samples_per_ms * frame_ms)
    bytes_per_chunk = samples_per_chunk * channels * bytes_per_sample

    idx = 0
    total = len(raw)
    while idx < total:
        chunk = raw[idx: idx + bytes_per_chunk]
        if len(chunk) < bytes_per_chunk:
            chunk = chunk + (b"\x00" * (bytes_per_chunk - len(chunk)))
        frame = AudioFrame(chunk, sample_rate, channels, samples_per_chunk)
        yield frame
        idx += bytes_per_chunk
        await asyncio.sleep(0)


class TranslatorAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a low-latency relay agent. Forward speech in each listener's language.")
        self.user_prefs: Dict[str, Dict[str, str]] = {}
        self._tts_sema = asyncio.Semaphore(MAX_CONCURRENT_TTS)

    async def on_enter(self):
        logger.info("[agent] joined session")
        try:
            voice = get_default_voice("hi-IN")
            tts_blob = await asyncio.to_thread(
                generate_speech_from_text,
                "Translator bot has joined the room.",
                language="hi-IN",
                voice=voice
            )
            if tts_blob:
                audio_iter = _bytes_to_audio_frames_async(tts_blob, sample_rate=44100, channels=1, frame_ms=FRAME_MS)
                await self.session.say("", audio=audio_iter)
        except Exception:
            logger.exception("[agent] failed to announce on enter")

    async def set_user_pref(self, user_id: str, language: str, voice: Optional[str] = None):
        if not language:
            language = "hi-IN"
        if not voice:
            voice = get_default_voice(language)
        self.user_prefs[user_id] = {"language": language, "voice": voice}
        logger.info(f"[agent] prefs set for {user_id} -> {language}, {voice}")

    async def _translate_and_play_for_target(self, recognized_text: str, from_lang: str, target_id: str, to_lang: str, voice: str, speaker_id: str):
        if target_id == speaker_id:
            return
        async with self._tts_sema:
            try:
                translated = await asyncio.to_thread(translate_text_murf, recognized_text, target_language=to_lang)
                if not translated:
                    return
                tts_blob = await asyncio.to_thread(generate_speech_from_text, translated, language=to_lang, voice=voice)
                if not tts_blob:
                    return

                audio_iter = _bytes_to_audio_frames_async(tts_blob, sample_rate=44100, channels=1, frame_ms=FRAME_MS)
                await self.session.say("", audio=audio_iter)
            except Exception:
                logger.exception("[agent] translate/tts failed for target %s", target_id)

    async def on_transcription(self, participant, track, result):
        """Called automatically by LiveKit STT whenever text is ready."""
        recognized = result.alternatives[0].text.strip()
        if not recognized:
            return

        speaker_id = participant.identity
        speaker_pref = self.user_prefs.get(speaker_id, {"language": "hi-IN", "voice": get_default_voice("hi-IN")})
        speaker_lang = speaker_pref["language"]

        logger.info("[agent] STT result for %s: %r", speaker_id, recognized)

        tasks = []
        for target_id, pref in self.user_prefs.items():
            if target_id == speaker_id:
                continue
            to_lang = pref["language"]
            voice = pref["voice"] or get_default_voice(to_lang)
            tasks.append(asyncio.create_task(
                self._translate_and_play_for_target(recognized, speaker_lang, target_id, to_lang, voice, speaker_id)
            ))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class RoomBotHandle:
    def __init__(self, room_code: str, url: str, api_key: str, api_secret: str):
        self.room_code = room_code
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self._agent = TranslatorAgent()
        self._session = AgentSession()
        self.identity = f"bot_{room_code}"
        self._lg = logger.getChild(self.identity)
        self._room: Optional[rtc.Room] = None
        self._tasks: List[asyncio.Task] = []
        self._closed = False

    def _mint_token(self) -> str:
        at = (
            AccessToken(self.api_key, self.api_secret)
            .with_identity(self.identity)
            .with_name("TranslatorBot")
            .with_grants(
                VideoGrants(
                    room_join=True,
                    room=self.room_code,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
        )
        return at.to_jwt()

    async def start(self):
        token = self._mint_token()
        self._room = rtc.Room()
        await self._room.connect(self.url, token, options=rtc.RoomOptions(auto_subscribe=True))

        # ✅ Attach OpenAI STT to session
        stt = openai.STT(model="gpt-4o-transcribe")
        await self._session.start(agent=self._agent, room=self._room, stt=stt)

    async def stop(self):
        if self._closed:
            return
        self._closed = True
        try:
            await self._session.aclose()
        except Exception:
            pass
        if self._room:
            try:
                await self._room.aclose()
            except Exception:
                pass
            self._room = None
        for t in self._tasks:
            if not t.done():
                try:
                    t.cancel()
                except Exception:
                    pass
        self._tasks.clear()


_bots: Dict[str, RoomBotHandle] = {}


async def ensure_room_bot(room_code: str, url: str, api_key: str, api_secret: str) -> RoomBotHandle:
    if room_code in _bots:
        return _bots[room_code]
    bot = RoomBotHandle(room_code, url, api_key, api_secret)
    _bots[room_code] = bot
    asyncio.create_task(bot.start())
    return bot


async def stop_room_bot(room_code: str):
    bot = _bots.pop(room_code, None)
    if bot:
        await bot.stop()
