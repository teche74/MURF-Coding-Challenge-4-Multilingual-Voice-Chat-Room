import asyncio
import json
import logging
import contextlib
import io

import numpy as np
from pydub import AudioSegment

log = logging.getLogger("bot")
import asyncio
import json
import logging
import contextlib
import io

import numpy as np
from pydub import AudioSegment

log = logging.getLogger("bot")

try:
    from livekit import rtc
except Exception as e:
    rtc = None
    log.warning("LiveKit rtc client not available: %s", e)

from backend.murf_pipeline import run_pipeline, process_audio_pipeline

_running: dict[str, "Bot"] = {}


class Bot:
    def __init__(self, room_code: str, url: str, token: str):
        self.room_code = room_code
        self.url = url
        self.token = token
        self.client: rtc.Room | None = None
        self.audio_sources: dict[str, rtc.AudioSource] = {}  # per-user
        self.local_tracks: dict[str, rtc.LocalAudioTrack] = {}
        self.closed = asyncio.Event()

        # voice preferences {user_id: {"language": str, "voice": str}}
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
                log.info("bot subscribed to %s", participant.identity)
                asyncio.create_task(self._consume_audio(track, participant.identity))

        await self.closed.wait()

    async def _consume_audio(self, remote_audio: "rtc.RemoteAudioTrack", speaker_id: str):
        """
        Stream speaker audio → Murf STT once → translate once per language →
        fan-out TTS per listener voice.
        """
        stream = await remote_audio.create_stream()

        async for frame in stream:
            pcm_bytes = frame.data
            try:
                # Step 1: Run STT once on speaker audio
                recognized = await asyncio.to_thread(
                    lambda: process_audio_pipeline(pcm_bytes, stt_lang="auto")[0]
                )
                if not recognized:
                    continue

                # Collect target languages from user_prefs
                lang_map: dict[str, list[str]] = {}
                for uid, pref in self.user_prefs.items():
                    target_lang = pref.get("language", "en-US")
                    if target_lang not in lang_map:
                        lang_map[target_lang] = []
                    lang_map[target_lang].append(uid)

                # Step 2: Translate once per unique target language
                for target_lang, user_ids in lang_map.items():
                    if target_lang == "auto":
                        continue

                    translated = await asyncio.to_thread(
                        lambda: process_audio_pipeline(
                            pcm_bytes, stt_lang="auto", target_lang=target_lang
                        )[1]
                    )
                    if not translated:
                        continue

                    # Step 3: Generate TTS per user voice
                    for uid in user_ids:
                        voice = self.user_prefs[uid].get("voice", "en-US-Wavenet-A")
                        _, _, tts_bytes = await asyncio.to_thread(
                            process_audio_pipeline,
                            pcm_bytes,
                            "auto",
                            target_lang,
                            voice,
                        )
                        if tts_bytes:
                            await self._play_tts(uid, tts_bytes)

            except Exception as e:
                log.warning("Pipeline error in bot %s: %s", self.room_code, e)

    async def _play_tts(self, user_id: str, tts_bytes: bytes):
        """
        Decode MP3 → PCM → feed into per-user track.
        """
        try:
            if user_id not in self.audio_sources:
                self.audio_sources[user_id] = rtc.AudioSource(48000, 1)
                self.local_tracks[user_id] = rtc.LocalAudioTrack.create_audio_track(
                    f"bot_audio_{user_id}", self.audio_sources[user_id]
                )
                await self.client.local_participant.publish_track(
                    self.local_tracks[user_id]
                )

            audio = AudioSegment.from_file(io.BytesIO(tts_bytes), format="mp3")
            audio = audio.set_frame_rate(48000).set_channels(1).set_sample_width(2)
            raw = audio.raw_data
            samples = np.frombuffer(raw, dtype=np.int16)

            frame_size = 960
            for i in range(0, len(samples), frame_size):
                chunk = samples[i : i + frame_size]
                if len(chunk) < frame_size:
                    break
                frame = rtc.AudioFrame(
                    data=chunk.tobytes(),
                    sample_rate=48000,
                    num_channels=1,
                )
                self.audio_sources[user_id].capture_frame(frame)
                await asyncio.sleep(0.02)
        except Exception as e:
            log.error("TTS playback failed for %s: %s", user_id, e)

    async def set_user_pref(self, user_id: str, language: str, voice: str):
        """Register/Update user's target language + voice preference."""
        self.user_prefs[user_id] = {"language": language, "voice": voice}

    async def stop(self):
        with contextlib.suppress(Exception):
            if self.client:
                await self.client.disconnect()
        self.closed.set()


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
            VideoGrants(
                room_join=True, room=room_code, can_publish=True, can_subscribe=True
            )
        )
    )
    token = at.to_jwt()

    bot = Bot(room_code, url, token)
    asyncio.create_task(bot.start())
    _running[room_code] = bot
    return bot


async def stop_room_bot(room_code: str):
    bot = _running.pop(room_code, None)
    if bot:
        await bot.stop()

try:
    from livekit import rtc
except Exception as e:
    rtc = None
    log.warning("LiveKit rtc client not available: %s", e)

from backend.murf_pipeline import run_pipeline

_running: dict[str, "Bot"] = {}


class Bot:
    def __init__(self, room_code: str, url: str, token: str):
        self.room_code = room_code
        self.url = url
        self.token = token
        self.client: rtc.Room | None = None
        self.audio_sources: dict[str, rtc.AudioSource] = {}  # per-user
        self.local_tracks: dict[str, rtc.LocalAudioTrack] = {}
        self.closed = asyncio.Event()

        # voice preferences {user_id: {"language": str, "voice": str}}
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
                log.info("bot subscribed to %s", participant.identity)
                asyncio.create_task(self._consume_audio(track, participant.identity))

        await self.closed.wait()

    async def _consume_audio(self, remote_audio: "rtc.RemoteAudioTrack", speaker_id: str):
        """
        Stream speaker audio → Murf STT once → fan-out TTS per listener preference.
        """
        stream = await remote_audio.create_stream()

        async for frame in stream:
            pcm_bytes = frame.data
            try:
                recognized, _, _ = await run_pipeline(
                    pcm_bytes, stt_lang="auto", target_lang="en-US"
                )
                if not recognized:
                    continue

                # Fan-out per listener
                for uid, pref in self.user_prefs.items():
                    lang = pref.get("language", "en-US")
                    voice = pref.get("voice", "en-US-Wavenet-A")
                    _, translated, tts_bytes = await run_pipeline(
                        pcm_bytes, stt_lang="auto", target_lang=lang, voice=voice
                    )
                    if tts_bytes:
                        await self._play_tts(uid, tts_bytes)
            except Exception as e:
                log.warning("Pipeline error: %s", e)

    async def _play_tts(self, user_id: str, tts_bytes: bytes):
        """
        Decode MP3 → PCM → feed into per-user track.
        """
        try:
            if user_id not in self.audio_sources:
                self.audio_sources[user_id] = rtc.AudioSource(48000, 1)
                self.local_tracks[user_id] = rtc.LocalAudioTrack.create_audio_track(
                    f"bot_audio_{user_id}", self.audio_sources[user_id]
                )
                await self.client.local_participant.publish_track(
                    self.local_tracks[user_id]
                )

            audio = AudioSegment.from_file(io.BytesIO(tts_bytes), format="mp3")
            audio = audio.set_frame_rate(48000).set_channels(1).set_sample_width(2)
            raw = audio.raw_data
            samples = np.frombuffer(raw, dtype=np.int16)

            frame_size = 960
            for i in range(0, len(samples), frame_size):
                chunk = samples[i : i + frame_size]
                if len(chunk) < frame_size:
                    break
                frame = rtc.AudioFrame(
                    data=chunk.tobytes(),
                    sample_rate=48000,
                    num_channels=1,
                )
                self.audio_sources[user_id].capture_frame(frame)
                await asyncio.sleep(0.02)
        except Exception as e:
            log.error("TTS playback failed: %s", e)

    async def set_user_pref(self, user_id: str, language: str, voice: str):
        """Register/Update user's target language + voice preference."""
        self.user_prefs[user_id] = {"language": language, "voice": voice}

    async def stop(self):
        with contextlib.suppress(Exception):
            if self.client:
                await self.client.disconnect()
        self.closed.set()


async def ensure_room_bot(room_code: str, url: str, api_key: str, api_secret: str):
    from livekit.api import AccessToken, VideoGrants

    if room_code in _running:
        return _running[room_code]

    identity = f"bot_{room_code}"
    at = (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name("Translator Bot")
        .with_grants(VideoGrants(room_join=True, room=room_code, can_publish=True, can_subscribe=True))
    )
    token = at.to_jwt()

    bot = Bot(room_code, url, token)
    asyncio.create_task(bot.start())
    _running[room_code] = bot
    return bot


async def stop_room_bot(room_code: str):
    bot = _running.pop(room_code, None)
    if bot:
        await bot.stop()
