from backend.murf_api import process_audio_pipeline
import asyncio
async def run_pipeline(audio_bytes: bytes, stt_lang="en-US", target_lang="es-ES", voice="es-ES-Wavenet-A"):
    recognized, translated, tts_bytes = await asyncio.to_thread(
        process_audio_pipeline, audio_bytes, stt_lang, target_lang, voice
    )
    return recognized, translated, tts_bytes