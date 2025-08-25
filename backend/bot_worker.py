import asyncio
import logging
import contextlib
import io
import time
from collections import defaultdict
import numpy as np
from pydub import AudioSegment

log = logging.getLogger("bot")

try:
    from livekit import rtc
except Exception as e:
    rtc = None
    log.warning("LiveKit rtc client not available: %s", e)

from backend.murf_pipeline import process_audio_pipeline

_running: dict[str, "Bot"] = {}


class Bot:
    # ---- Tunables for streaming-ish STT + VAD ----
    DEFAULT_SR = 48000          # expected sample rate
    DEFAULT_CH = 1              # mono
    SAMPLE_WIDTH = 2            # 16-bit PCM
    MIN_CHUNK_MS = 400          # don't flush tiny chunks
    MAX_CHUNK_MS = 1500         # upper bound to keep latency reasonable
    SILENCE_RMS = 350           # energy threshold for silence (int16 RMS)
    SILENCE_HOLD_MS = 400       # how long of silence before early flush

    def __init__(self, room_code: str, url: str, token: str):
        self.room_code = room_code
        self.url = url
        self.token = token
        self.client: rtc.Room | None = None
        self.audio_sources: dict[str, rtc.AudioSource] = {}
        self.local_tracks: dict[str, rtc.LocalAudioTrack] = {}

        self.playback_queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self.playback_tasks: dict[str, asyncio.Task] = {}

        self.closed = asyncio.Event()
        self.user_prefs: dict[str, dict] = {}

        self.buffers: dict[str, bytearray] = defaultdict(bytearray)
        self.sr: dict[str, int] = defaultdict(lambda: self.DEFAULT_SR)
        self.ch: dict[str, int] = defaultdict(lambda: self.DEFAULT_CH)
        self.last_voice_time: dict[str, float] = {}   # last non-silent frame timestamp
        self.last_flush_time: dict[str, float] = {}   # last time we flushed a chunk

    async def start(self):
        if rtc is None:
            raise RuntimeError("LiveKit RTC not installed")

        self.client = rtc.Room()
        await self.client.connect(self.url, self.token)
        log.info("bot[%s] connected", self.room_code)

        def _on_subscribed(track, publication, participant):
            if participant.identity.startswith("bot_"):
                return
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                log.info("bot[%s] subscribed to %s", self.room_code, participant.identity)

                def _on_frame(frame: rtc.AudioFrame):
                    asyncio.create_task(self._ingest_frame(frame, participant.identity))

                track.on("frame_received", _on_frame)

        self.client.on("track_subscribed", _on_subscribed)

        await self.closed.wait()

    async def stop(self):
        with contextlib.suppress(Exception):
            if self.client:
                await self.client.disconnect()
        for task in self.playback_tasks.values():
            task.cancel()
        self.closed.set()
        log.info("bot[%s] stopped", self.room_code)

    async def _ingest_frame(self, frame: "rtc.AudioFrame", speaker_id: str):
        """
        Collect frames into ~speech chunks; flush:
          - when enough audio gathered (MAX_CHUNK_MS)
          - or when we detect sustained silence for SILENCE_HOLD_MS
        """
        now = time.time()
        sr = getattr(frame, "sample_rate", self.DEFAULT_SR)
        ch = getattr(frame, "num_channels", self.DEFAULT_CH)
        data: bytes = frame.data

        self.sr[speaker_id] = sr
        self.ch[speaker_id] = ch

        buf = self.buffers[speaker_id]
        buf.extend(data)

        if len(data) >= 2:
            samples = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        else:
            rms = 0.0

        if rms > self.SILENCE_RMS:
            self.last_voice_time[speaker_id] = now
        elif speaker_id not in self.last_voice_time:
            self.last_voice_time[speaker_id] = now

        buf_ms = self._bytes_to_ms(len(buf), sr, ch)
        time_since_voice = (now - self.last_voice_time.get(speaker_id, now)) * 1000.0
        time_since_flush = (now - self.last_flush_time.get(speaker_id, 0.0)) * 1000.0

        should_flush_for_silence = (
            buf_ms >= self.MIN_CHUNK_MS and time_since_voice >= self.SILENCE_HOLD_MS
        )
        should_flush_for_size = buf_ms >= self.MAX_CHUNK_MS

        if should_flush_for_silence or should_flush_for_size:
            await self._flush_buffer(speaker_id)

    def _bytes_to_ms(self, nbytes: int, sr: int, ch: int) -> float:

        if sr <= 0 or ch <= 0:
            sr = self.DEFAULT_SR
            ch = self.DEFAULT_CH
        samples = nbytes / (self.SAMPLE_WIDTH * ch)
        ms = (samples / sr) * 1000.0
        return ms

    async def _flush_buffer(self, speaker_id: str):
        """Copy + clear to avoid blocking ingestion; process chunk asynchronously."""
        buf = self.buffers[speaker_id]
        if not buf:
            return
        sr = self.sr[speaker_id]
        ch = self.ch[speaker_id]

        pcm_bytes = bytes(buf)
        buf.clear()

        self.last_flush_time[speaker_id] = time.time()

        asyncio.create_task(self._process_chunk(pcm_bytes, speaker_id, sr, ch))

    async def _process_chunk(self, pcm_bytes: bytes, speaker_id: str, sr: int, ch: int):
        """
        Emulates streaming STT using short chunks through your existing Murf pipeline:
          - First pass: STT only (target=None, voice=None)
          - Fan-out per listener: translation + TTS
        """
        try:
            recognized, _, _ = await asyncio.to_thread(
                process_audio_pipeline, pcm_bytes, "auto", None, None
            )
            if not recognized:
                return
            log.debug("bot[%s] recognized='%s'", self.room_code, recognized)

            if not self.user_prefs:
                return

            for uid, pref in self.user_prefs.items():
                if uid == speaker_id:
                    continue
                target_lang = pref.get("language", "en-US")
                voice = pref.get("voice", "en-US-Wavenet-A")

                _, _, tts_bytes = await asyncio.to_thread(
                    process_audio_pipeline, pcm_bytes, "auto", target_lang, voice
                )

                if tts_bytes:
                    log.info(
                        "bot[%s] queueing TTS for %s lang=%s voice=%s",
                        self.room_code, uid, target_lang, voice,
                    )
                    await self._play_tts(uid, tts_bytes)

        except Exception as e:
            log.error("Pipeline error in bot %s: %s", self.room_code, e, exc_info=True)

    async def _play_tts(self, user_id: str, tts_bytes: bytes):
        await self.playback_queues[user_id].put(tts_bytes)
        if user_id not in self.playback_tasks or self.playback_tasks[user_id].done():
            self.playback_tasks[user_id] = asyncio.create_task(self._process_queue(user_id))

    async def _process_queue(self, user_id: str):
        while True:
            try:
                tts_bytes = await self.playback_queues[user_id].get()
                await self._play_tts_bytes(user_id, tts_bytes)
            except Exception as e:
                log.error("Playback queue error for %s: %s", user_id, e, exc_info=True)
            finally:
                self.playback_queues[user_id].task_done()

    async def _play_tts_bytes(self, user_id: str, tts_bytes: bytes):
        """Publish an audio track per listener and write frames sequentially."""
        try:
            track_name = f"bot_audio_{self.room_code}_{user_id}"
            if track_name not in self.audio_sources:
                self.audio_sources[track_name] = rtc.AudioSource(self.DEFAULT_SR, self.DEFAULT_CH)
                self.local_tracks[track_name] = rtc.LocalAudioTrack.create_audio_track(
                    track_name, self.audio_sources[track_name]
                )
                await self.client.local_participant.publish_track(self.local_tracks[track_name])
                log.info("bot[%s] published new track for %s", self.room_code, user_id)

            audio = AudioSegment.from_file(io.BytesIO(tts_bytes), format="mp3")
            audio = audio.set_frame_rate(self.DEFAULT_SR).set_channels(self.DEFAULT_CH).set_sample_width(self.SAMPLE_WIDTH)
            raw = audio.raw_data
            samples = np.frombuffer(raw, dtype=np.int16)

            # 20ms frames @ 48kHz mono: 960 samples
            frame_size = 960
            for i in range(0, len(samples), frame_size):
                chunk = samples[i:i + frame_size]
                if len(chunk) < frame_size:
                    break
                frame = rtc.AudioFrame(
                    data=chunk.tobytes(),
                    sample_rate=self.DEFAULT_SR,
                    num_channels=self.DEFAULT_CH,
                )
                self.audio_sources[track_name].capture_frame(frame)
                await asyncio.sleep(0.02)  

        except Exception as e:
            log.error("TTS playback failed for %s: %s", user_id, e, exc_info=True)

    async def set_user_pref(self, user_id: str, language: str, voice: str):
        self.user_prefs[user_id] = {"language": language, "voice": voice}
        log.info(
            "bot[%s] set user_pref for %s: language=%s voice=%s",
            self.room_code, user_id, language, voice,
        )


async def ensure_room_bot(room_code: str, url: str, api_key: str, api_secret: str):
    from livekit.api import AccessToken, VideoGrants

    if room_code in _running:
        return _running[room_code]

    identity = f"bot_{room_code}"
    at = (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name("Translator Bot")
        .with_grants(
            VideoGrants(room_join=True, room=room_code, can_publish=True, can_subscribe=True)
        )
    )
    token = at.to_jwt()

    bot = Bot(room_code, url, token)
    asyncio.create_task(bot.start())
    _running[room_code] = bot
    log.info("bot[%s] started", room_code)
    return bot


async def stop_room_bot(room_code: str):
    bot = _running.pop(room_code, None)
    if bot:
        await bot.stop()
