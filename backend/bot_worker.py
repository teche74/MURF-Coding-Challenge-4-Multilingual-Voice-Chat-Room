"""
Improved LiveKit Translator bot worker (backend/bot_worker.py).

Key features:
- Correct AgentSession usage (connect rtc.Room with token, then start AgentSession with room=room).
- Per-participant audio reader with simple silence-based VAD to detect end-of-turn.
- Uses asyncio.to_thread() for blocking Murf SDK calls (STT/Translate/TTS).
- Concurrency limit (Semaphore) for TTS tasks to avoid overloading Murf.
- Robust start/stop lifecycle with logging and error handling.

Dependencies:
- livekit (python SDK with agents + rtc)
- pydub (+ ffmpeg installed on machine)
- murf SDK (your existing backend.murf_api module)

Drop this file into your `backend/` folder and ensure your FastAPI app imports `ensure_room_bot`/`stop_room_bot` from here.

"""

import asyncio
import io
import logging
import os
import time
import struct
from typing import Dict, Optional, List

from dotenv import load_dotenv

# LiveKit imports
from livekit.agents.voice import Agent, AgentSession
from livekit import rtc
from livekit.api import AccessToken, VideoGrants
from livekit.rtc.audio_frame import AudioFrame

# audio helpers
from pydub import AudioSegment

# Your existing Murf helpers
from backend.murf_api import (
    speech_to_text_murf,
    translate_text_murf,
    generate_speech_from_text,
)

logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(os.path.dirname(BASE_DIR), ".env")
load_dotenv(ENV)

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")

# tuneables
SILENCE_THRESHOLD = 500  # RMS threshold for silence detection (tune for your audio)
SILENCE_SECONDS_TO_END = 0.7  # how long silence to wait before treating as end-of-turn
MIN_SPEECH_BYTES = 16000 * 2 // 5  # ~0.2s of 16kHz 16-bit mono
MAX_CONCURRENT_TTS = 3  # limit concurrent TTS calls to avoid API throttling
FRAME_MS = 20  # output TTS frame size when streaming back into LiveKit


# --------------------------
# helpers
# --------------------------

def _rms_of_pcm16(pcm_bytes: bytes) -> float:
    """Compute a quick RMS of int16 PCM bytes."""
    if not pcm_bytes:
        return 0.0
    # iterate over 16-bit little-endian samples
    # use struct to unpack in chunks to avoid huge memory churn
    count = len(pcm_bytes) // 2
    if count == 0:
        return 0.0
    fmt = f"<{count}h"
    try:
        samples = struct.unpack(fmt, pcm_bytes[: count * 2])
    except Exception:
        # fallback to simple loop
        sm = 0
        for i in range(0, len(pcm_bytes) - 1, 2):
            s = int.from_bytes(pcm_bytes[i : i + 2], "little", signed=True)
            sm += s * s
        return (sm / max(1, count)) ** 0.5
    sm = 0
    for s in samples:
        sm += s * s
    return (sm / count) ** 0.5


async def _bytes_to_audio_frames_async(audio_bytes: bytes, sample_rate: int = 44100, channels: int = 1, frame_ms: int = FRAME_MS):
    """Async generator that yields livekit.rtc.AudioFrame objects from encoded audio bytes (mp3/wav/etc.).

    Note: uses pydub to decode the container. pydub requires ffmpeg in PATH.
    """
    # decode to PCM using pydub
    audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
    audio = audio.set_frame_rate(sample_rate).set_channels(channels).set_sample_width(2)  # 16-bit
    raw = audio.raw_data

    bytes_per_sample = 2
    samples_per_ms = sample_rate / 1000.0
    samples_per_chunk = int(samples_per_ms * frame_ms)
    bytes_per_chunk = samples_per_chunk * channels * bytes_per_sample

    idx = 0
    total = len(raw)
    while idx < total:
        chunk = raw[idx : idx + bytes_per_chunk]
        if len(chunk) < bytes_per_chunk:
            chunk = chunk + (b"\x00" * (bytes_per_chunk - len(chunk)))
        frame = AudioFrame(chunk, sample_rate, channels, samples_per_chunk)
        yield frame
        idx += bytes_per_chunk
        # give control back to loop
        await asyncio.sleep(0)


# --------------------------
# Agent that orchestrates translate/tts
# --------------------------
class TranslatorAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a low-latency relay agent. Forward speech in each listener's language.")
        self.user_prefs: Dict[str, Dict[str, str]] = {}
        # semaphore limits concurrent TTS tasks
        self._tts_sema = asyncio.Semaphore(MAX_CONCURRENT_TTS)

    async def on_enter(self):
        logger.info("[agent] joined session")
        # announce presence (best-effort)
        try:
            await self.session.say("Translator bot has joined the room.")
        except Exception:
            logger.exception("[agent] failed to announce on enter")

    async def set_user_pref(self, user_id: str, language: str, voice: Optional[str] = None):
        if not language:
            language = "en-US"
        if not voice:
            voice = "en-US-Wavenet-D"
        self.user_prefs[user_id] = {"language": language, "voice": voice}
        logger.info(f"[agent] prefs set for {user_id} -> {language}, {voice}")

    async def _translate_and_play_for_target(self, recognized_text: str, from_lang: str, target_id: str, to_lang: str, voice: str):
        """Translate recognized_text to to_lang, synthesize audio and stream into session.say(audio=...)."""
        # throttle concurrent TTS calls
        async with self._tts_sema:
            try:
                translated = await asyncio.to_thread(translate_text_murf, recognized_text, target_language=to_lang)
                if not translated:
                    logger.warning("[agent] empty translation for target %s", target_id)
                    return

                tts_blob = await asyncio.to_thread(generate_speech_from_text, translated, language=to_lang, voice=voice)
                if not tts_blob:
                    logger.warning("[agent] empty TTS blob for target %s", target_id)
                    return

                # stream audio back to room
                audio_iter = _bytes_to_audio_frames_async(tts_blob, sample_rate=44100, channels=1, frame_ms=FRAME_MS)
                # say supports audio iterator; text can be empty when streaming audio
                try:
                    await self.session.say("", audio=audio_iter)
                except Exception:
                    logger.exception("[agent] failed to say audio for target %s", target_id)
            except Exception:
                logger.exception("[agent] translate/tts failed for target %s", target_id)

    async def handle_speech_chunk(self, pcm_bytes: bytes, sample_rate: int, speaker_id: str):
        """Take a completed speech chunk (PCM16LE bytes) and fan-out translations to other participants."""
        if not pcm_bytes:
            return
        speaker_pref = self.user_prefs.get(speaker_id, {"language": "en-US", "voice": "en-US-Wavenet-D"})
        speaker_lang = speaker_pref.get("language", "en-US")

        # STT (blocking) -> run in thread
        try:
            recognized = await asyncio.to_thread(speech_to_text_murf, pcm_bytes, sample_rate, speaker_lang)
        except Exception:
            logger.exception("[agent] STT failed for speaker %s", speaker_id)
            return

        if not recognized:
            logger.debug("[agent] no text recognized for speaker %s", speaker_id)
            return

        # fan out - spawn tasks for each listener (except speaker)
        tasks = []
        for target_id, pref in self.user_prefs.items():
            if target_id == speaker_id:
                continue
            to_lang = pref.get("language", "en-US")
            voice = pref.get("voice", "en-US-Wavenet-D")
            tasks.append(asyncio.create_task(self._translate_and_play_for_target(recognized, speaker_lang, target_id, to_lang, voice)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------
# RoomBotHandle - lifecycle & track reader
# --------------------------
class RoomBotHandle:
    def __init__(self, room_code: str, url: str, api_key: str, api_secret: str):
        self.room_code = room_code
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self._agent = TranslatorAgent()
        self._session = AgentSession()
        self.identity = f"bot_{room_code}"
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
        logger.info("[bot] starting for room %s", self.room_code)
        token = self._mint_token()

        # create & connect room
        self._room = rtc.Room()
        try:
            await self._room.connect(self.url, token)
            logger.info("[bot] connected to room %s", getattr(self._room, "name", self.room_code))
        except Exception:
            logger.exception("[bot] failed to connect room %s", self.room_code)
            self._room = None
            return

        # start agent session with room so RoomIO is created
        try:
            await self._session.start(agent=self._agent, room=self._room)
            logger.info("[bot] AgentSession started for room %s", self.room_code)
        except Exception:
            logger.exception("[bot] AgentSession.start failed for room %s", self.room_code)
            try:
                await self._room.aclose()
            except Exception:
                pass
            self._room = None
            return

        # hook: when audio track subscribed
        @self._room.on("track_subscribed")
        def _on_track_subscribed(track, publication, participant):
            # spawn a reader task
            if getattr(track, "kind", None) != "audio":
                return

            t = asyncio.create_task(self._read_track_loop(track, participant))
            self._tasks.append(t)

        logger.info("[bot] ready and listening in room %s", self.room_code)

    async def _read_track_loop(self, track, participant):
        """Read audio chunks from a subscribed track and detect end-of-turn via simple silence-based logic."""
        logger.info("[bot] started reader for participant %s", getattr(participant, "identity", "<unknown>"))
        buffer = bytearray()
        last_voice_time = time.time()
        sample_rate = 16000

        try:
            async for chunk in track.read_audio_chunks():
                try:
                    data = chunk.data.tobytes()
                    sr = getattr(chunk, "sample_rate", None) or sample_rate
                except Exception:
                    # if track yields raw bytes directly
                    data = bytes(chunk)
                    sr = sample_rate

                buffer.extend(data)
                # compute rms of last 200ms of buffer to check voice
                window_bytes = buffer[-(int(0.2 * sr) * 2) :]
                rms = _rms_of_pcm16(bytes(window_bytes))

                if rms > SILENCE_THRESHOLD:
                    last_voice_time = time.time()

                # If buffer large enough or silence timeout reached, treat as end-of-turn and send for processing
                if len(buffer) >= (sr * 2 * 1) or (time.time() - last_voice_time) > SILENCE_SECONDS_TO_END:
                    pcm_snapshot = bytes(buffer)
                    buffer.clear()
                    # run processing but don't block reader loop
                    asyncio.create_task(self._process_speech_chunk(pcm_snapshot, sr, participant.identity))

        except Exception:
            logger.exception("[bot] read loop failed for participant %s", getattr(participant, "identity", "<unknown>"))

    async def _process_speech_chunk(self, pcm_bytes: bytes, sample_rate: int, speaker_id: str):
        # delegate to agent handler
        try:
            await self._agent.handle_speech_chunk(pcm_bytes, sample_rate, speaker_id)
        except Exception:
            logger.exception("[bot] processing speech chunk failed for %s", speaker_id)

    async def stop(self):
        if self._closed:
            return
        self._closed = True
        logger.info("[bot] stopping for room %s", self.room_code)
        try:
            await self._session.aclose()
        except Exception:
            logger.exception("[bot] closing session failed")

        if self._room:
            try:
                await self._room.aclose()
            except Exception:
                logger.exception("[bot] closing room failed")
            self._room = None

        # cancel background tasks
        for t in self._tasks:
            if not t.done():
                t.cancel()
        self._tasks.clear()

    async def set_user_pref(self, user_id: str, language: str, voice: Optional[str] = None):
        # update in agent
        try:
            await self._agent.set_user_pref(user_id, language, voice)
        except Exception:
            logger.exception("[bot] set_user_pref failed for %s", user_id)


# --------------------------
# module-level helpers used by your FastAPI backend
# --------------------------
_bots: Dict[str, RoomBotHandle] = {}


async def ensure_room_bot(room_code: str, url: str, api_key: str, api_secret: str) -> RoomBotHandle:
    """Ensure a RoomBotHandle exists and is starting in background."""
    if room_code in _bots:
        return _bots[room_code]

    bot = RoomBotHandle(room_code, url, api_key, api_secret)
    _bots[room_code] = bot
    # start as background task and log exceptions
    async def _starter():
        try:
            await bot.start()
        except Exception:
            logger.exception("[bot] background start failed for %s", room_code)

    asyncio.create_task(_starter())
    return bot


async def stop_room_bot(room_code: str):
    bot = _bots.pop(room_code, None)
    if bot:
        await bot.stop()
        logger.info("[bot] stopped for room %s", room_code)


# end of file
