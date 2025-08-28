"""
Improved LiveKit Translator bot worker (backend/bot_worker.py).

This variant includes comprehensive end-to-end logging to aid debugging.

Key features:
- Detailed DEBUG logs at every major step, including token minting, LiveKit connect, AgentSession start,
  track subscription, audio chunk reception, RMS values, STT/Translate/TTS steps and frame streaming.
- Contextual child loggers per room/bot identity to filter logs per-room easily.
- Safeguards: MIN_SPEECH_BYTES check before STT, try/except around likely failure points.

Drop this file into your `backend/` folder replacing the previous worker file.
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
from livekit.rtc import AudioStream

# audio helpers
from pydub import AudioSegment

# Your existing Murf helpers
from backend.murf_api import (
    speech_to_text_whisper,
    translate_text_murf,
    generate_speech_from_text,
    get_default_voice,
)

# --------------------------
# logging setup (verbose by default for debugging)
# --------------------------
logger = logging.getLogger("bot")
# attach a stream handler if none exists so logs appear in stdout when run under uvicorn etc.
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

# tuneables
SILENCE_THRESHOLD = 100  # RMS threshold for silence detection (tune for your audio)
SILENCE_SECONDS_TO_END = 0.5  # how long silence to wait before treating as end-of-turn
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
    logger.debug("[tts.frames] starting decode: bytes=%d, sample_rate=%s, channels=%s, frame_ms=%s",
                 len(audio_bytes) if audio_bytes else 0, sample_rate, channels, frame_ms)
    # decode to PCM using pydub
    try:
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
    except Exception as e:
        logger.exception("[tts.frames] pydub failed to decode TTS bytes")
        raise

    try:
        audio = audio.set_frame_rate(sample_rate).set_channels(channels).set_sample_width(2)  # 16-bit
        raw = audio.raw_data
    except Exception as e:
        logger.exception("[tts.frames] failed to normalize audio: %s", e)
        raise

    bytes_per_sample = 2
    samples_per_ms = sample_rate / 1000.0
    samples_per_chunk = int(samples_per_ms * frame_ms)
    bytes_per_chunk = samples_per_chunk * channels * bytes_per_sample

    idx = 0
    total = len(raw)
    frame_count = 0
    while idx < total:
        chunk = raw[idx : idx + bytes_per_chunk]
        if len(chunk) < bytes_per_chunk:
            chunk = chunk + (b"\x00" * (bytes_per_chunk - len(chunk)))
        frame = AudioFrame(chunk, sample_rate, channels, samples_per_chunk)
        frame_count += 1
        logger.debug("[tts.frames] yielding frame %d (bytes=%d)", frame_count, len(chunk))
        yield frame
        idx += bytes_per_chunk
        # give control back to loop
        await asyncio.sleep(0)
    logger.debug("[tts.frames] finished yielding %d frames", frame_count)


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
        try:
            voice = get_default_voice("hi-IN")
            logger.debug("[agent] generating join announcement TTS (voice=%s)", voice)
            tts_blob = await asyncio.to_thread(
                generate_speech_from_text,
                "Translator bot has joined the room.",
                language="hi-IN",
                voice=voice
            )
            logger.debug("[agent] join TTS blob len=%s", len(tts_blob) if tts_blob else None)
            if tts_blob:
                audio_iter = _bytes_to_audio_frames_async(tts_blob, sample_rate=44100, channels=1, frame_ms=FRAME_MS)
                try:
                    await self.session.say("", audio=audio_iter)
                    logger.info("[agent] announced join message via session.say()")
                except Exception:
                    logger.exception("[agent] failed to say announcement audio on enter")
        except Exception:
            logger.exception("[agent] failed to announce on enter")
    
    async def set_user_pref(self, user_id: str, language: str, voice: Optional[str] = None):
        if not language:
            language = "hi-IN"
        if not voice:
            voice = get_default_voice(language)
        self.user_prefs[user_id] = {"language": language, "voice": voice}
        logger.info(f"[agent] prefs set for {user_id} -> {language}, {voice}")

    async def _translate_and_play_for_target(self, recognized_text: str, from_lang: str, target_id: str, to_lang: str, voice: str):
        """Translate recognized_text to to_lang, synthesize audio and stream into session.say(audio=...)."""
        # throttle concurrent TTS calls
        logger.debug("[agent] translate_and_play start: target=%s, to_lang=%s, voice=%s", target_id, to_lang, voice)
        async with self._tts_sema:
            try:
                translated = await asyncio.to_thread(translate_text_murf, recognized_text, target_language=to_lang)
                logger.info("[agent] translated for %s -> %s : %r", target_id, to_lang, translated)
                if not translated:
                    logger.warning("[agent] empty translation for target %s", target_id)
                    return

                tts_blob = await asyncio.to_thread(generate_speech_from_text, translated, language=to_lang, voice=voice)
                logger.info("[agent] tts bytes len for %s : %s", target_id, len(tts_blob) if tts_blob else "None")
                if not tts_blob:
                    logger.warning("[agent] empty TTS blob for target %s", target_id)
                    return

                audio_iter = _bytes_to_audio_frames_async(tts_blob, sample_rate=44100, channels=1, frame_ms=FRAME_MS)
                try:
                    logger.debug("[agent] calling session.say() for target %s", target_id)
                    await self.session.say("", audio=audio_iter)
                    logger.info("[agent] successfully streamed audio for target %s", target_id)
                except Exception:
                    logger.exception("[agent] failed to say audio for target %s", target_id)
            except Exception:
                logger.exception("[agent] translate/tts failed for target %s", target_id)

    async def handle_speech_chunk(self, pcm_bytes: bytes, sample_rate: int, speaker_id: str):
        """Take a completed speech chunk (PCM16LE bytes) and fan-out translations to other participants."""
        logger.debug("[agent] handle_speech_chunk called: speaker=%s bytes=%d sample_rate=%s", speaker_id, len(pcm_bytes) if pcm_bytes else 0, sample_rate)
        if not pcm_bytes:
            logger.debug("[agent] empty pcm_bytes for %s - skipping", speaker_id)
            return

        speaker_pref = self.user_prefs.get(speaker_id, {"language": "hi-IN", "voice": get_default_voice("hi-IN")})
        speaker_lang = speaker_pref.get("language", "en-US")

        # ensure chunk meets minimum size expectation
        if len(pcm_bytes) < MIN_SPEECH_BYTES:
            logger.debug("[agent] pcm_bytes too small (%d < %d) - skipping STT", len(pcm_bytes), MIN_SPEECH_BYTES)
            return

        try:
            logger.debug("[agent] calling STT for speaker=%s (lang=%s)", speaker_id, speaker_lang)
            recognized = await asyncio.to_thread(speech_to_text_whisper, pcm_bytes, sample_rate, speaker_lang)
            logger.info("[agent] STT result for %s: %r", speaker_id, recognized)
        except Exception:
            logger.exception("[agent] STT failed for speaker %s", speaker_id)
            return

        if not recognized:
            logger.debug("[agent] no text recognized for speaker %s", speaker_id)
            return

        tasks = []
        for target_id, pref in self.user_prefs.items():
            if target_id == speaker_id:
                continue
            to_lang = pref.get("language", "hi-IN")
            voice = pref.get("voice") or get_default_voice(to_lang)
            logger.debug("[agent] queuing translation for target=%s to_lang=%s", target_id, to_lang)
            tasks.append(asyncio.create_task(self._translate_and_play_for_target(recognized, speaker_lang, target_id, to_lang, voice)))

        if tasks:
            logger.debug("[agent] awaiting %d translation tasks for speaker=%s", len(tasks), speaker_id)
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.debug("[agent] translation tasks completed for speaker=%s", speaker_id)


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
        # create a child logger for contextual logs per-room/bot
        self._lg = logger.getChild(self.identity)
        self._room: Optional[rtc.Room] = None
        self._tasks: List[asyncio.Task] = []
        self._closed = False
        self._lg.debug("RoomBotHandle initialized")

    def _mint_token(self) -> str:
        self._lg.debug("_mint_token called")
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
        token = at.to_jwt()
        self._lg.debug("_mint_token produced token length=%d", len(token) if token else 0)
        return token

    async def start(self):
        self._lg.info("[bot] starting for room %s", self.room_code)
        token = None
        try:
            token = self._mint_token()
        except Exception:
            self._lg.exception("[bot] token minting failed for room %s", self.room_code)
            return

        # create & connect room
        self._room = rtc.Room()
        try:
            self._lg.debug("[bot] connecting to LiveKit url=%s", self.url)
            await self._room.connect(self.url, 
            token,
            options=rtc.RoomOptions(auto_subscribe=True) 
            )
            self._lg.info("[bot] connected to room %s", getattr(self._room, "name", self.room_code))
        except Exception:
            self._lg.exception("[bot] failed to connect room %s", self.room_code)
            self._room = None
            return

        # start agent session with room so RoomIO is created
        try:
            self._lg.debug("[bot] starting AgentSession")
            await self._session.start(agent=self._agent, room=self._room)
            self._lg.info("[bot] AgentSession started for room %s", self.room_code)
        except Exception:
            self._lg.exception("[bot] AgentSession.start failed for room %s", self.room_code)
            try:
                await self._room.aclose()
            except Exception:
                pass
            self._room = None
            return

        # hook: when audio track subscribed
        @self._room.on("track_subscribed")
        def _on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
            async def handle():
                try:
                    if track.kind != rtc.TrackKind.KIND_AUDIO:
                        self._lg.debug("[bot.on] non-audio track subscribed -> ignoring")
                        return

                    self._lg.info("[bot.on] audio track subscribed from %s", participant.identity)
                    stream = AudioStream(track)
                    # Use the silence-based reader loop
                    asyncio.create_task(self._read_track_loop(stream, participant))

                except Exception:
                    self._lg.exception("[bot.on] exception in track_subscribed handler")
            
            asyncio.create_task(handle())

    
    async def _read_track_loop(self, stream: AudioStream, participant):
        """Read audio frames from a subscribed track and detect end-of-turn via simple silence-based logic."""
        self._lg.info("[bot] started reader for participant %s", getattr(participant, "identity", "<unknown>"))
        buffer = bytearray()
        last_voice_time = time.time()
        sample_rate = 48000
        min_speech_bytes = int(sample_rate * 2 * 0.5)

        try:
            async for frame_event in stream:
                
                audio_frame = frame_event.frame   
                data = bytes(audio_frame.data )
                sr = audio_frame.sample_rate or sample_rate
                
                if not isinstance(data, (bytes, bytearray)):
                    self._lg.warning("[bot.read] received non-bytes data (type=%s) - skipping", type(data))
                    continue

                buffer.extend(data)

                # compute rms of last 200ms of buffer
                window_bytes = buffer[-(int(0.2 * sr) * 2):]
                rms = _rms_of_pcm16(bytes(window_bytes))

                self._lg.debug(
                    "[bot.read] frame received participant=%s sr=%s chunk_len=%d buffer_len=%d rms=%.2f",
                    getattr(participant, 'identity', None), sr, len(data), len(buffer), rms
                )

                if rms > SILENCE_THRESHOLD:
                    last_voice_time = time.time()

                # End-of-turn check
                if (len(buffer) >= min_speech_bytes) or ((time.time() - last_voice_time) > SILENCE_SECONDS_TO_END and len(buffer) > 0):
                    pcm_snapshot = bytes(buffer)
                    buffer.clear()

                    self._lg.debug(
                        "[bot.read] silence detected or max buffer reached, snapshot_len=%d, sending to STT",
                        len(pcm_snapshot)
                    )

                    if len(pcm_snapshot) < MIN_SPEECH_BYTES:
                        self._lg.debug("[bot.read] snapshot too small (%d < %d) - ignoring", len(pcm_snapshot), MIN_SPEECH_BYTES)
                        continue

                    asyncio.create_task(self._process_speech_chunk(pcm_snapshot, sr, participant.identity))

        except Exception:
            self._lg.exception("[bot] read loop failed for participant %s", getattr(participant, "identity", "<unknown>"))
    
    async def _process_speech_chunk(self, pcm_bytes: bytes, sample_rate: int, speaker_id: str):
        # delegate to agent handler
        self._lg.debug("[bot.proc] _process_speech_chunk called speaker=%s bytes=%d sample_rate=%s", speaker_id, len(pcm_bytes) if pcm_bytes else 0, sample_rate)
        try:
            await self._agent.handle_speech_chunk(pcm_bytes, sample_rate, speaker_id)
            self._lg.debug("[bot.proc] agent.handle_speech_chunk completed for %s", speaker_id)
        except Exception:
            self._lg.exception("[bot] processing speech chunk failed for %s", speaker_id)

    async def stop(self):
        if self._closed:
            return
        self._closed = True
        self._lg.info("[bot] stopping for room %s", self.room_code)
        try:
            await self._session.aclose()
            self._lg.debug("[bot] AgentSession closed")
        except Exception:
            self._lg.exception("[bot] closing session failed")

        if self._room:
            try:
                await self._room.aclose()
                self._lg.debug("[bot] room closed")
            except Exception:
                self._lg.exception("[bot] closing room failed")
            self._room = None

        # cancel background tasks
        for t in self._tasks:
            if not t.done():
                try:
                    t.cancel()
                except Exception:
                    self._lg.exception("[bot] failed to cancel task")
        self._tasks.clear()
        self._lg.info("[bot] stopped cleanup complete for room %s", self.room_code)

    async def set_user_pref(self, user_id: str, language: str, voice: Optional[str] = None):
        # update in agent
        try:
            self._lg.debug("[bot] set_user_pref called: %s %s %s", user_id, language, voice)
            await self._agent.set_user_pref(user_id, language, voice)
            self._lg.debug("[bot] set_user_pref completed for %s", user_id)
        except Exception:
            self._lg.exception("[bot] set_user_pref failed for %s", user_id)


# --------------------------
# module-level helpers used by your FastAPI backend
# --------------------------
_bots: Dict[str, RoomBotHandle] = {}


async def ensure_room_bot(room_code: str, url: str, api_key: str, api_secret: str) -> RoomBotHandle:
    """Ensure a RoomBotHandle exists and is starting in background."""
    logger.debug("ensure_room_bot called for %s", room_code)
    if room_code in _bots:
        logger.debug("ensure_room_bot: existing bot found for %s", room_code)
        return _bots[room_code]

    bot = RoomBotHandle(room_code, url, api_key, api_secret)
    _bots[room_code] = bot
    logger.info("ensure_room_bot: created bot handle for %s", room_code)
    # start as background task and log exceptions
    async def _starter():
        try:
            logger.debug("_starter: starting bot.start() for %s", room_code)
            await bot.start()
            logger.info("_starter: bot.start() completed for %s", room_code)
        except Exception:
            logger.exception("[bot] background start failed for %s", room_code)

    asyncio.create_task(_starter())
    return bot


async def stop_room_bot(room_code: str):
    logger.debug("stop_room_bot called for %s", room_code)
    bot = _bots.pop(room_code, None)
    if bot:
        await bot.stop()
        logger.info("[bot] stopped for room %s", room_code)
    else:
        logger.debug("stop_room_bot: no bot to stop for %s", room_code)

