from fastapi import FastAPI, WebSocket, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
import random, string, logging, os, sys, asyncio, json, time
from typing import Dict, List, Optional,Tuple
from authlib.integrations.starlette_client import OAuth
from starlette.config import Config
from starlette.responses import RedirectResponse

sys.path.append(os.path.dirname(__file__))
from speech_to_text_and_translation_utils import speech_to_text, translate_text
from murf_api import generate_speech_from_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
config = Config(env_path)
CLIENT_ID = config('GOOGLE_CLIENT_ID', default=None)
CLIENT_SECRET = config('GOOGLE_CLIENT_SECRET', default=None)

oauth = OAuth(config)
oauth.register(
    name='google',
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    jwks_uri='https://www.googleapis.com/oauth2/v3/certs',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'email openid'}
)

users: Dict[str, Dict] = {}
rooms: Dict[str, Dict] = {}
room_connections: Dict[str, List[WebSocket]] = {}
ws_to_user: Dict[WebSocket, str] = {}
MAX_ROOM_CAPACITY = 4
TTS_CACHE: Dict[Tuple[str, str, str], Tuple[bytes, float]] = {}
CACHE_TTL_SECONDS = 60 * 5


class CreateRoomRequest(BaseModel):
    user_id: str
    public: bool = True

class JoinRoomRequest(BaseModel):
    user_id: str
    room_code: Optional[str] = None

app = FastAPI(title="Multilingual Chat Room")
app.add_middleware(SessionMiddleware, secret_key=config('SESSION_SECRET_KEY', default="super-secret"))
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

def generate_room_code(length: int = 6) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

@app.get("/room_info")
def room_info(room_code: str):
    room = rooms.get(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return {"members": room["members"]}

async def synthesize_with_cache(text: str, target_lang: str, voice: str="default"):
    key = (text, target_lang, voice)
    now = time.time()
    entry = TTS_CACHE.get(key)
    if entry and now - entry[1] < CACHE_TTL_SECONDS:
        return entry[0]
    audio_bytes = await asyncio.to_thread(generate_speech_from_text, text, target_lang, voice)
    TTS_CACHE[key] = (audio_bytes, now)
    return audio_bytes

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket , room_code: str = Query(...), user_id: str = Query(...)):
    if user_id not in users:
        await websocket.close(code=4001, reason="User not found")
        return
    if room_code not in rooms or user_id not in rooms[room_code]["members"]:
        await websocket.close(code=4002, reason="Room not found or user not in room")
        return

    await websocket.accept()
    room_connections.setdefault(room_code, []).append(websocket)
    ws_to_user[websocket] = user_id
    logger.info(f"{user_id} connected to room {room_code}")

    try:
        while True:
            try:
                msg = await websocket.receive()
                if msg is None:
                    break
                if "bytes" in msg and msg["bytes"] is not None:
                    audio_blob = msg["bytes"]
                elif "text" in msg and msg["text"] is not None:
                    continue
                else:
                    continue
            except Exception as e:
                logger.warning(f"WebSocket receive failed: {e}")
                break

            try:
                for ws in list(room_connections.get(room_code, [])):
                    if ws is websocket:
                        continue
                    try:
                        await ws.send_bytes(audio_blob)
                    except Exception as e:
                        logger.warning(f"Failed to forward raw audio to a client: {e}")
            except Exception:
                pass

            async def handle_transcription_and_tts(blob, sender_ws, roomcode):
                try:
                    recognized_text = await asyncio.to_thread(speech_to_text, blob)
                except Exception as e:
                    logger.warning(f"Speech-to-text failed: {e}")
                    return

                if not recognized_text:
                    return

                lang_to_ws: Dict[str, List[WebSocket]] = {}
                for ws in list(room_connections.get(roomcode, [])):
                    uid = ws_to_user.get(ws)
                    if not uid:
                        continue
                    user_lang = users.get(uid, {}).get("language", "en")
                    lang_to_ws.setdefault(user_lang, []).append(ws)

                translations = {}
                for target_lang in list(lang_to_ws.keys()):
                    try:
                        translated = await asyncio.to_thread(translate_text, recognized_text, target_lang)
                        translations[target_lang] = translated
                    except Exception:
                        translations[target_lang] = recognized_text

                for tgt_lang, ws_list in lang_to_ws.items():
                    text_for_lang = translations.get(tgt_lang) or recognized_text
                    if not text_for_lang:
                        continue
                    try:
                        mp3_bytes = await synthesize_with_cache(text_for_lang, tgt_lang)
                    except Exception as e:
                        logger.warning(f"Murf synthesis failed: {e}")
                        for w in ws_list:
                            try:
                                await w.send_text(json.dumps({"type":"error","message":"tts_failed"}))
                            except:
                                pass
                        continue

                    header = {
                        "type": "tts_audio",
                        "from": ws_to_user.get(sender_ws, "unknown"),
                        "target_lang": tgt_lang,
                        "text": text_for_lang,
                        "format": "mp3"
                    }
                    header_json = json.dumps(header)
                    for w in ws_list:
                        try:
                            await w.send_text(header_json)
                            await w.send_bytes(mp3_bytes)
                        except Exception as e:
                            logger.warning(f"Failed to send TTS to a client: {e}")

            asyncio.create_task(handle_transcription_and_tts(audio_blob, websocket, room_code))

    finally:
        if room_code in room_connections and websocket in room_connections[room_code]:
            room_connections[room_code].remove(websocket)
        ws_to_user.pop(websocket, None)
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"{user_id} disconnected from room {room_code}")

@app.get("/login/google")
async def login_google(request: Request):
    redirect_uri = request.url_for('auth_callback')
    return await oauth.google.authorize_redirect(request, redirect_uri, access_type="offline")

@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    try:
        user_info = await oauth.google.parse_id_token(request, token, nonce=None, claims_options={"iss": {"essential": False}})
    except Exception:
        user_info = await oauth.google.userinfo(token=token)

    user_email = user_info.get('email')
    if not user_email:
        raise HTTPException(status_code=400, detail="Email not found")

    users[user_email] = {"name": user_email.split('@')[0], "language": "en"}
    frontend_url = f"http://localhost:8501/?user_id={user_email}&name={users[user_email]['name']}"
    return RedirectResponse(frontend_url)

@app.post("/create_room")
def create_room(req: CreateRoomRequest):
    if req.user_id not in users:
        raise HTTPException(status_code=400, detail="User not found")
    room_code = generate_room_code()
    rooms[room_code] = {"members": [req.user_id], "public": req.public}
    return {"status": "success", "room_code": room_code}

@app.post("/join_room")
def join_room(req: JoinRoomRequest):
    if req.user_id not in users:
        raise HTTPException(status_code=400, detail="User not found")

    if req.room_code:
        room = rooms.get(req.room_code)
        if not room:
            raise HTTPException(status_code=400, detail="Room not found")
        if len(room["members"]) >= MAX_ROOM_CAPACITY:
            raise HTTPException(status_code=400, detail="Room full")
        if req.user_id not in room["members"]:
            room["members"].append(req.user_id)
        return {"status": "success", "room_code": req.room_code}
    else:
        public_rooms = [code for code, r in rooms.items() if r["public"] and len(r["members"]) < MAX_ROOM_CAPACITY]
        if not public_rooms:
            raise HTTPException(status_code=400, detail="No public rooms available")
        selected_code = random.choice(public_rooms)
        rooms[selected_code]["members"].append(req.user_id)
        return {"status": "success", "room_code": selected_code}
