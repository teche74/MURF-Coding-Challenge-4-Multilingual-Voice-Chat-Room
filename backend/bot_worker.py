import asyncio
import json
import logging
import os
from typing import Dict, Optional, List

from dotenv import load_dotenv

from livekit.agents.voice import Agent, AgentSession
from livekit.api import AccessToken, VideoGrants

# Import your Murf pipeline
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

# --------------------------
# Translator Agent
# --------------------------
class TranslatorAgent(Agent):
    """
    Uses Murf for STT -> Translate -> TTS.
    """
    def __init__(self) -> None:
        super().__init__(
            instructions="You are a low-latency relay agent. Forward speech in each listener's language."
        )
        # { user_id: {"language": "en-US", "voice": "en-US-Wavenet-D"} }
        self.user_prefs: Dict[str, Dict[str, str]] = {}

    async def on_enter(self):
        await self.session.say("Murf relay joined. I will auto-translate everyone to their preferred language.")

    async def set_user_pref(self, user_id: str, language: str, voice: Optional[str] = None):
        if not language:
            language = "en-US"
        if not voice:
            voice = "en-US-Wavenet-D"
        self.user_prefs[user_id] = {"language": language, "voice": voice}
        logger.info(f"[agent] prefs set for {user_id} -> {language}, {voice}")

    async def on_audio_track(self, track, participant):
        speaker_id = participant.identity
        speaker_pref = self.user_prefs.get(speaker_id, {"language": "en-US", "voice": "en-US-Wavenet-D"})
        speaker_lang = speaker_pref.get("language", "en-US")

        logger.info(f"[agent] listening to {speaker_id} (lang={speaker_lang})")

        async for chunk in track.read_audio_chunks():
            pcm = chunk.data.tobytes()  # raw PCM16 from LiveKit
            sample_rate = chunk.sample_rate

            # 1. STT (Murf)
            try:
                recognized = speech_to_text_murf(pcm, sample_rate=sample_rate, language=speaker_lang)
            except Exception as e:
                logger.warning(f"[agent] STT failed: {e}")
                continue

            if not recognized:
                continue

            # 2. Fan out translations
            tasks: List[asyncio.Task] = []
            for target_id, target_pref in self.user_prefs.items():
                if target_id == speaker_id:
                    continue
                target_lang = target_pref.get("language", "en-US")
                target_voice = target_pref.get("voice", "en-US-Wavenet-D")

                tasks.append(asyncio.create_task(
                    self._translate_and_play(recognized, from_lang=speaker_lang, to_lang=target_lang, voice=target_voice, exclude=[speaker_id])
                ))

            if tasks:
                await asyncio.gather(*tasks)

    async def _translate_and_play(self, text: str, from_lang: str, to_lang: str, voice: str, exclude: Optional[List[str]] = None):
        try:
            translated = translate_text_murf(text, target_language=to_lang)
            if not translated:
                return

            audio_bytes = generate_speech_from_text(translated, language=to_lang, voice=voice)
            if not audio_bytes:
                return

            # Play into LiveKit session
            await self.session.play_audio(audio_bytes, exclude=exclude or [])
        except Exception as e:
            logger.warning(f"[agent] translate+play failed: {e}")

# --------------------------
# Entrypoint & Bot Handle
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
        logger.info(f"[bot] starting Murf session for room {self.room_code}")
        await self._session.start(
            agent=self._agent,
            token=token
        )
        logger.info(f"[bot] started Murf session for room {self.room_code}")

    async def stop(self):
        logger.info(f"[bot] stopping session for room {self.room_code}")
        try:
            await self._session.close()
        except Exception:
            pass

    async def set_user_pref(self, user_id: str, language: str, voice: Optional[str] = None):
        await self._agent.set_user_pref(user_id, language, voice)


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
        logger.info(f"[bot] stopped for room {room_code}")