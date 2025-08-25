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
        self.closed = asyncio.Event()
        self.user_prefs: dict[str, dict] = {}  # {user_id: {"language": str, "voice": str}}

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
                # Step 1: Speech-to-text
                recognized = await asyncio.to_thread(
                    lambda: process_audio_pipeline(pcm_bytes, stt_lang="auto")[0]
                )
                log.debug("bot[%s] recognized='%s'", self.room_code, recognized)

                if not recognized:
                    continue

                if not self.user_prefs:
                    log.warning("bot[%s] has no user_prefs set, skipping fanout", self.room_code)
                    continue

                # Collect target languages
                lang_map: dict[str, list[str]] = {}
                for uid, pref in self.user_prefs.items():
                    target_lang = pref.get("language", "en-US")
                    lang_map.setdefault(target_lang, []).append(uid)

                log.info("bot[%s] fanout languages=%s", self.room_code, list(lang_map.keys()))

                # Step 2: Translate + TTS
                for target_lang, user_ids in lang_map.items():
                    translated = await asyncio.to_thread(
                        lambda: process_audio_pipeline(
                            pcm_bytes, stt_lang="auto", target_lang=target_lang
                        )[1]
                    )
                    log.debug("bot[%s] translated(%s)='%s'", self.room_code, target_lang, translated)

                    if not translated:
                        continue

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
                            log.info(
                                "bot[%s] playing TTS for user %s lang=%s voice=%s",
                                self.room_code,
                                uid,
                                target_lang,
                                voice,
                            )
                            await self._play_tts(uid, tts_bytes)
                        else:
                            log.warning("bot[%s] failed TTS for user %s", self.room_code, uid)

            except Exception as e:
                log.error("Pipeline error in bot %s: %s", self.room_code, e, exc_info=True)

    async def _play_tts(self, user_id: str, tts_bytes: bytes):
        log.debug(
            "bot[%s] preparing TTS playback for user %s (%d bytes)",
            self.room_code,
            user_id,
            len(tts_bytes) if tts_bytes else 0,
        )
        try:
            if user_id not in self.audio_sources:
                self.audio_sources[user_id] = rtc.AudioSource(48000, 1)
                self.local_tracks[user_id] = rtc.LocalAudioTrack.create_audio_track(
                    f"bot_audio_{user_id}", self.audio_sources[user_id]
                )
                await self.client.local_participant.publish_track(
                    self.local_tracks[user_id]
                )
                log.info("bot[%s] published new track for %s", self.room_code, user_id)

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
            log.error("TTS playback failed for %s: %s", user_id, e, exc_info=True)

    async def set_user_pref(self, user_id: str, language: str, voice: str):
        self.user_prefs[user_id] = {"language": language, "voice": voice}
        log.info(
            "bot[%s] set user_pref for %s: language=%s voice=%s",
            self.room_code,
            user_id,
            language,
            voice,
        )

    async def stop(self):
        with contextlib.suppress(Exception):
            if self.client:
                await self.client.disconnect()
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
