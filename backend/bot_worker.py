import asyncio
import asyncio
import logging
from livekit.agents.voice import AgentSession
from backend.murf_pipeline import run_pipeline

log = logging.getLogger("bot")


class TranslatorBot:
    def __init__(self, room_url, api_key, api_secret, room_code):
        self.room_url = room_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.room_code = room_code
        self.session = None
        # {user_id: {"language": "en-US", "voice": "en-US-Wavenet-D"}}
        self.user_langs = {}

    async def start(self):
        self.session = AgentSession()
        await self.session.connect(
            url=self.room_url,
            api_key=self.api_key,
            api_secret=self.api_secret,
            identity=f"bot_{self.room_code}",
            room=self.room_code,
        )
        logger.info(f"Translator bot joined room {self.room_code}")

        @self.session.on_audio_track
        async def on_audio_track(track, participant):
            user_id = participant.identity
            prefs = self.user_langs.get(user_id)
            if not prefs:
                logger.warning(f"No prefs for {user_id}, skipping audio")
                return

            source_lang = prefs["language"]
            logger.info(f"Received audio from {user_id} ({source_lang})")

            async for chunk in track.read_audio_chunks():
                # Process audio once (STT → Translate text once)
                recognized, _, _ = await run_pipeline(
                    chunk,
                    stt_lang=source_lang,
                    target_lang=source_lang,  # just transcribe
                )
                if not recognized:
                    continue

                # Now generate TTS separately for each other participant
                tasks = []
                for target_id, target_prefs in self.user_langs.items():
                    if target_id == user_id:
                        continue  # don't send back to speaker

                    target_lang = target_prefs["language"]
                    target_voice = target_prefs["voice"]

                    tasks.append(self._translate_and_play(
                        recognized,
                        from_lang=source_lang,
                        to_lang=target_lang,
                        voice=target_voice,
                        exclude=[user_id]
                    ))
                await asyncio.gather(*tasks)

    async def _translate_and_play(self, text, from_lang, to_lang, voice, exclude):
        """Translate text into target language and play audio."""
        if from_lang == to_lang:
            translated_text = text
        else:
            _, translated_text, tts_bytes = await run_pipeline(
                text.encode("utf-8"),
                stt_lang=from_lang,
                target_lang=to_lang,
                voice=voice,
            )
            if not translated_text or not tts_bytes:
                return
            await self.session.play_audio(tts_bytes, exclude=exclude)
            return

        # If no translation needed (same language), just echo
        tts_bytes = await run_pipeline(
            text.encode("utf-8"),
            stt_lang=from_lang,
            target_lang=to_lang,
            voice=voice,
        )
        if tts_bytes:
            await self.session.play_audio(tts_bytes, exclude=exclude)

    async def set_user_pref(self, user_id, language, voice):
        self.user_langs[user_id] = {"language": language, "voice": voice}
        logger.info(f"Set prefs for {user_id}: {language}, {voice}")

# --------------------------
# Entrypoint helpers
# --------------------------

async def ensure_room_bot(room_code, url, api_key, api_secret):
    bot = TranslatorBot(url, api_key, api_secret, room_code)
    task = asyncio.create_task(bot.start())
    return bot

async def stop_room_bot(room_code):
    # Optional cleanup
    logger.info(f"Stopping translator bot for room {room_code}")