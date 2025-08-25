import asyncio
import logging
import contextlib
import io
import numpy as np
from pydub import AudioSegment
from collections import defaultdict

log = logging.getLogger("bot")

try:
    from livekit import rtc
except Exception as e:
    rtc = None
    log.warning("LiveKit rtc client not available: %s", e)

from backend.murf_pipeline import process_audio_pipeline

_running: dict[str, "Bot"] = {}


class Bot:
    def __init__(self, room_code: str, url: str, token: str):
        self.room_code = room_code
        self.url = url
        self.token = token
        self.client: rtc.Room | None = None
        self.audio_sources: dict[str, rtc.AudioSource] = {}
        self.local_tracks: dict[str, rtc.LocalAudioTrack] = {}

        # per-user TTS queues + tasks
        self.playback_queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self.playback_tasks: dict[str, asyncio.Task] = {}

        self.closed = asyncio.Event()
        self.user_prefs: dict[str, dict] = {}

    async def start(self):
        if rtc is None:
            raise RuntimeError("LiveKit RTC not installed")

        self.client = rtc.Room()
        await self.client.connect(self.url, self.token)
        log.info("bot[%s] connected", self.room_code)

        @self.client.on("track_subscribed")
        async def _on_subscribed(track, publication, participant):
            if participant.identity.startswith("bot_"):
                return
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                log.info("bot[%s] subscribed to %s", self.room_code, participant.identity)
                asyncio.create_task(self._consume_audio(track, participant.identity))

        await self.closed.wait()

    async def _consume_audio(self, remote_audio: "rtc.RemoteAudioTrack", speaker_id: str):
        log.info("bot[%s] consuming audio from speaker %s", self.room_code, speaker_id)
        stream = await remote_audio.create_stream()

        async for frame in stream:
            pcm_bytes = frame.data
            try:
                # Run full pipeline once
                recognized, translated, _ = await asyncio.to_thread(
                    process_audio_pipeline, pcm_bytes, "auto", None, None
                )
                log.debug("bot[%s] recognized='%s'", self.room_code, recognized)

                if not recognized or not self.user_prefs:
                    continue

                for uid, pref in self.user_prefs.items():
                    if uid == speaker_id:
                        continue
                    target_lang = pref.get("language", "en-US")
                    voice = pref.get("voice", "en-US-Wavenet-A")

                    # Run full pipeline for translation+TTS
                    _, translated, tts_bytes = await asyncio.to_thread(
                        process_audio_pipeline, pcm_bytes, "auto", target_lang, voice
                    )

                    if tts_bytes:
                        log.info(
                            "bot[%s] queueing TTS for user %s lang=%s voice=%s",
                            self.room_code, uid, target_lang, voice,
                        )
                        await self._play_tts(uid, tts_bytes)
                    else:
                        log.warning("bot[%s] failed TTS for user %s", self.room_code, uid)

            except Exception as e:
                log.error("Pipeline error in bot %s: %s", self.room_code, e, exc_info=True)

    async def _play_tts(self, user_id: str, tts_bytes: bytes):
        await self.playback_queues[user_id].put(tts_bytes)

        if user_id not in self.playback_tasks or self.playback_tasks[user_id].done():
            self.playback_tasks[user_id] = asyncio.create_task(self._process_queue(user_id))

    async def _process_queue(self, user_id: str):
        """Worker that plays queued TTS sequentially for one user."""
        while True:
            try:
                tts_bytes = await self.playback_queues[user_id].get()
                await self._play_tts_bytes(user_id, tts_bytes)
            except Exception as e:
                log.error("Playback queue error for %s: %s", user_id, e, exc_info=True)
            finally:
                self.playback_queues[user_id].task_done()

    async def _play_tts_bytes(self, user_id: str, tts_bytes: bytes):
        """Actual audio playback logic."""
        log.debug(
            "bot[%s] playing queued TTS for user %s (%d bytes)",
            self.room_code, user_id, len(tts_bytes) if tts_bytes else 0,
        )
        try:
            track_name = f"bot_audio_{self.room_code}_{user_id}"

            if track_name not in self.audio_sources:
                self.audio_sources[track_name] = rtc.AudioSource(48000, 1)
                self.local_tracks[track_name] = rtc.LocalAudioTrack.create_audio_track(
                    track_name, self.audio_sources[track_name]
                )
                await self.client.local_participant.publish_track(
                    self.local_tracks[track_name]
                )
                log.info("bot[%s] published new track for %s", self.room_code, user_id)

            audio = AudioSegment.from_file(io.BytesIO(tts_bytes), format="mp3")
            audio = audio.set_frame_rate(48000).set_channels(1).set_sample_width(2)
            raw = audio.raw_data
            samples = np.frombuffer(raw, dtype=np.int16)

            frame_size = 960
            for i in range(0, len(samples), frame_size):
                chunk = samples[i: i + frame_size]
                if len(chunk) < frame_size:
                    break
                frame = rtc.AudioFrame(
                    data=chunk.tobytes(), sample_rate=48000, num_channels=1,
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

    async def stop(self):
        with contextlib.suppress(Exception):
            if self.client:
                await self.client.disconnect()
        # cancel playback workers
        for task in self.playback_tasks.values():
            task.cancel()
        self.closed.set()
        log.info("bot[%s] stopped", self.room_code)


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
