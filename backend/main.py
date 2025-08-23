import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from authlib.integrations.starlette_client import OAuth
from starlette.responses import RedirectResponse
import random, string, logging, os, sys, asyncio, time
from typing import Dict, Optional, Tuple
from dotenv import load_dotenv
from backend.speech_to_text_and_translation_utils import speech_to_text, translate_text
from backend.murf_api import generate_speech_from_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(env_path)

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

oauth = OAuth()
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
MAX_ROOM_CAPACITY = 4
TTS_CACHE: Dict[Tuple[str, str, str], Tuple[bytes, float]] = {}
CACHE_TTL_SECONDS = 60 * 5

app = FastAPI(title="Multilingual Chat Room")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", "super-secret"))
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

def generate_room_code(length: int = 6) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

async def synthesize_with_cache(text: str, target_lang: str, voice: str="default"):
    key = (text, target_lang, voice)
    now = time.time()
    entry = TTS_CACHE.get(key)
    if entry and now - entry[1] < CACHE_TTL_SECONDS:
        return entry[0]
    audio_bytes = await asyncio.to_thread(generate_speech_from_text, text, target_lang, voice)
    TTS_CACHE[key] = (audio_bytes, now)
    return audio_bytes

class CreateRoomRequest(BaseModel):
    user_id: str
    public: bool = True

class JoinRoomRequest(BaseModel):
    user_id: str
    room_code: Optional[str] = None

class LeaveRoomRequest(BaseModel):
    user_id: str
    room_code: str

@app.get("/room_info")
def room_info(room_code: str):
    room = rooms.get(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return {"members": room["members"]}

@app.post("/create_room")
def create_room(req: CreateRoomRequest):
    if req.user_id not in users:
        raise HTTPException(status_code=400, detail="User not found")
    room_code = generate_room_code()
    rooms[room_code] = {"members": [req.user_id], "public": req.public}
    logger.info(f"room created {room_code} by {req.user_id}")
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

@app.post("/leave_room")
def leave_room(req: LeaveRoomRequest):
    room = rooms.get(req.room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if req.user_id in room["members"]:
        room["members"].remove(req.user_id)
        logger.info(f"user {req.user_id} left room {req.room_code} via API")
    return {"status": "success"}

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
    frontend_url = f"https://chatfree.streamlit.app/?user_id={user_email}&name={users[user_email]['name']}"
    return RedirectResponse(frontend_url)

@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...), target_lang: str = "en"):
    audio_bytes = await file.read()
    try:
        recognized_text = await asyncio.to_thread(speech_to_text, audio_bytes)
    except Exception as e:
        logger.warning(f"Speech-to-text failed: {e}")
        recognized_text = ""
    if not recognized_text:
        recognized_text = ""
    try:
        translated_text = await asyncio.to_thread(translate_text, recognized_text, target_lang)
    except Exception:
        translated_text = recognized_text
    try:
        tts_audio = await synthesize_with_cache(translated_text, target_lang)
    except Exception as e:
        logger.warning(f"TTS generation failed: {e}")
        tts_audio = b""
    import base64
    tts_base64 = base64.b64encode(tts_audio).decode("utf-8")
    return {"text": translated_text, "tts_audio_base64": tts_base64}

class WSRoomManager:
    def __init__(self):
        self.rooms_ws: Dict[str, Dict[str, WebSocket]] = {}

    async def connect(self, room_code: str, user_id: str, ws: WebSocket):
        await ws.accept()
        self.rooms_ws.setdefault(room_code, {})
        self.rooms_ws[room_code][user_id] = ws
        logger.info(f"WS connect {user_id} -> {room_code} (total {len(self.rooms_ws[room_code])})")

    def disconnect(self, room_code: str, user_id: str):
        if room_code in self.rooms_ws:
            self.rooms_ws[room_code].pop(user_id, None)
            if not self.rooms_ws[room_code]:
                self.rooms_ws.pop(room_code, None)
        logger.info(f"WS disconnect {user_id} from {room_code}")

    async def peers_in_room(self, room_code: str, exclude: str = "") -> Dict[str, WebSocket]:
        return {uid: sock for uid, sock in self.rooms_ws.get(room_code, {}).items() if uid != exclude}

    async def send_json(self, ws: WebSocket, payload: dict):
        await ws.send_json(payload)

    async def relay(self, room_code: str, target_user: str, payload: dict):
        target_ws = self.rooms_ws.get(room_code, {}).get(target_user)
        if target_ws:
            await target_ws.send_json(payload)

ws_manager = WSRoomManager()

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    room_code = websocket.query_params.get("room_code")
    user_id = websocket.query_params.get("user_id")

    if not room_code or not user_id:
        await websocket.close()
        return

    room = rooms.get(room_code)
    if not room or user_id not in room["members"]:
        await websocket.close()
        return

    await ws_manager.connect(room_code, user_id, websocket)

    peers = list((await ws_manager.peers_in_room(room_code, exclude=user_id)).keys())
    await ws_manager.send_json(websocket, {"type": "peers", "peers": peers})

    for peer_id, peer_ws in (await ws_manager.peers_in_room(room_code, exclude=user_id)).items():
        try:
            await ws_manager.send_json(peer_ws, {"type": "peer-joined", "user_id": user_id})
        except Exception:
            pass

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            to_user = msg.get("to")
            data = msg.get("data", {})
            if mtype in ("offer", "answer", "ice-candidate") and to_user:
                await ws_manager.relay(room_code, to_user, {
                    "type": mtype,
                    "from": user_id,
                    "data": data
                })
    except WebSocketDisconnect:
        logger.info(f"Websocket disconnect for {user_id}")
    except Exception as e:
        logger.exception("ws error: %s", e)
    finally:
        for peer_id, peer_ws in (await ws_manager.peers_in_room(room_code, exclude=user_id)).items():
            try:
                await ws_manager.send_json(peer_ws, {"type": "peer-left", "user_id": user_id})
            except Exception:
                pass
        ws_manager.disconnect(room_code, user_id)

        room = rooms.get(room_code)
        if room and user_id in room["members"]:
            try:
                room["members"].remove(user_id)
                logger.info(f"Removed {user_id} from room {room_code} after websocket close")
            except Exception:
                pass
