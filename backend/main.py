import sys
import os
import time
import random
import string
import logging
import asyncio
import base64

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from typing import Dict, Optional, Tuple
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from authlib.integrations.starlette_client import OAuth
from urllib.parse import quote
from dotenv import load_dotenv

try:
    from livekit.api import AccessToken, VideoGrants
except Exception:
    AccessToken = None
    VideoGrants = None

from backend.speech_to_text_and_translation_utils import speech_to_text, translate_text
from backend.murf_api import generate_speech_from_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(env_path)

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://chatfree.streamlit.app")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
oauth = OAuth()
if CLIENT_ID and CLIENT_SECRET:
    oauth.register(
        name='google',
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        jwks_uri='https://www.googleapis.com/oauth2/v3/certs',
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'email openid'}
    )

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")

if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL):
    logger.warning("LiveKit credentials not set. Set LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_URL in .env")

users: Dict[str, Dict] = {}
rooms: Dict[str, Dict] = {}
MAX_ROOM_CAPACITY = 4
TTS_CACHE: Dict[Tuple[str, str, str], Tuple[bytes, float]] = {}
CACHE_TTL_SECONDS = 60 * 5

app = FastAPI(title="Multilingual Chat Room")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", "super-secret"))
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)


def generate_room_code(length: int = 6) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def get_user_name(user_id: str) -> str:
    return users.get(user_id, {}).get("name", user_id)


def ensure_user(user_id: str, language: str = "en"):
    """Best-effort auto-upsert so restarts don't break room join/create."""
    if user_id not in users:
        name = (user_id.split("@")[0] if "@" in user_id else user_id)[:32]
        users[user_id] = {"name": name, "language": language}
    else:
        users[user_id].setdefault("language", language)


async def synthesize_with_cache(text: str, target_lang: str, voice: str = "default"):
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
    language: Optional[str] = "en"


class JoinRoomRequest(BaseModel):
    user_id: str
    room_code: Optional[str] = None
    language: Optional[str] = "en"


class LeaveRoomRequest(BaseModel):
    user_id: str
    room_code: str


class LiveKitJoinTokenReq(BaseModel):
    room_code: str
    user_id: str
    name: Optional[str] = None
    language: Optional[str] = "en"


@app.get("/room_info")
def room_info(room_code: str):
    room = rooms.get(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return {"members": room["members"]}


@app.post("/create_room")
def create_room(req: CreateRoomRequest):
    ensure_user(req.user_id, req.language or "en")
    room_code = generate_room_code()
    rooms[room_code] = {"members": [{"user_id": req.user_id, "language": req.language or "en"}], "public": req.public}
    logger.info(f"room created {room_code} by {req.user_id}")
    return {"status": "success", "room_code": room_code}


@app.post("/join_room")
def join_room(req: JoinRoomRequest):
    ensure_user(req.user_id, req.language or "en")

    if req.room_code:
        room = rooms.get(req.room_code)
        if not room:
            raise HTTPException(status_code=400, detail="Room not found")
        if len(room["members"]) >= MAX_ROOM_CAPACITY:
            raise HTTPException(status_code=400, detail="Room full")
        if not any(m["user_id"] == req.user_id for m in room["members"]):
            room["members"].append({"user_id": req.user_id, "language": req.language or "en"})
        return {"status": "success", "room_code": req.room_code}
    else:
        public_rooms = [code for code, r in rooms.items() if r["public"] and len(r["members"]) < MAX_ROOM_CAPACITY]
        if not public_rooms:
            raise HTTPException(status_code=400, detail="No public rooms available")
        selected_code = random.choice(public_rooms)
        if not any(m["user_id"] == req.user_id for m in rooms[selected_code]["members"]):
            rooms[selected_code]["members"].append({"user_id": req.user_id, "language": req.language or "en"})
        return {"status": "success", "room_code": selected_code}


@app.post("/leave_room")
def leave_room(req: LeaveRoomRequest):
    room = rooms.get(req.room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    room["members"] = [m for m in room["members"] if m["user_id"] != req.user_id]
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
    frontend_url = f"{FRONTEND_URL}/?user_id={quote(user_email)}&name={quote(users[user_email]['name'])}"
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
    tts_base64 = base64.b64encode(tts_audio).decode("utf-8")
    return {"text": translated_text, "tts_audio_base64": tts_base64}


@app.post("/livekit/join-token")
def livekit_join_token(req: LiveKitJoinTokenReq):
    """
    Returns a LiveKit access token and LiveKit URL. Client will use these to connect to LiveKit (SFU) directly.
    - req.room_code: string
    - req.user_id: string
    - req.name: optional display name
    - req.language: optional language preference (stored server-side)
    """
    if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL):
        raise HTTPException(500, "LiveKit is not configured on server.")

    ensure_user(req.user_id, req.language or "en")
    room = rooms.get(req.room_code)
    if not room:
        rooms[req.room_code] = {"members": [{"user_id": req.user_id, "language": req.language or "en"}], "public": True}
    else:
        if not any(m["user_id"] == req.user_id for m in room["members"]):
            room["members"].append({"user_id": req.user_id, "language": req.language or "en"})
        else:
            for m in room["members"]:
                if m["user_id"] == req.user_id:
                    m["language"] = req.language or m.get("language", "en")

    if AccessToken is None or VideoGrants is None:
        raise HTTPException(500, "LiveKit server SDK is not available on the server (check pip install).")
    
    try:
        at = AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET).with_identity(req.user_id).with_name(req.name or req.user_id).with_grants(
            VideoGrants(room_join=True, room=req.room_code, can_publish=True, can_subscribe=True, can_publish_data=True)
        )
        token_jwt = at.to_jwt()
    except Exception as e:
        logger.error(f"Failed to generate token: {e}")
        raise HTTPException(500, f"LiveKit token generation failed: {str(e)}")
    return {"token": token_jwt, "url": LIVEKIT_URL, "room_code": req.room_code}


ROOM_HTML = """
<!DOCTYPE html>
<html lang="en">

<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Multilingual Voice Room</title>
    <style>
        :root {
            --bg1: #0f2027;
            --bg2: #2c5364;
            --accent: #0ff;
        }

        body {
            margin: 0;
            font-family: 'Segoe UI', sans-serif;
            color: #eee;
            background: linear-gradient(135deg, var(--bg1), #203a43, var(--bg2));
            height: 100vh;
            display: flex;
        }

        .app-container {
            display: flex;
            width: 100%;
        }

        .sidebar {
            width: 270px;
            padding: 20px;
            background: rgba(0, 0, 0, 0.45);
            border-right: 1px solid rgba(255, 255, 255, 0.06);
        }

        .room-title {
            color: var(--accent);
            font-weight: 600;
            margin-bottom: 8px;
        }

        ul.participants-list {
            list-style: none;
            padding: 0;
            margin: 0 0 12px 0;
            max-height: 40vh;
            overflow: auto;
        }

        ul.participants-list li {
            padding: 8px;
            border-radius: 8px;
            margin-bottom: 6px;
            background: rgba(255, 255, 255, 0.03);
            display: flex;
            justify-content: space-between;
        }

        .me-badge {
            background: var(--accent);
            color: #000;
            padding: 2px 6px;
            border-radius: 6px;
            font-size: 12px;
            margin-left: 6px;
        }

        .main {
            flex: 1;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .status {
            text-align: center;
            color: var(--accent);
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 18px;
        }

        .tile {
            height: 160px;
            border-radius: 14px;
            background: rgba(255, 255, 255, 0.03);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 12px;
        }

        .tile.speaking {
            box-shadow: 0 0 18px rgba(0, 255, 255, 0.12);
            transform: scale(1.02);
        }

        .avatar {
            width: 72px;
            height: 72px;
            border-radius: 50%;
            background: rgba(255, 255, 255, 0.12);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 28px;
            margin-bottom: 8px;
        }

        .controls {
            display: flex;
            gap: 12px;
            justify-content: center;
            margin-top: 6px;
        }

        button {
            padding: 10px 14px;
            border-radius: 10px;
            border: none;
            cursor: pointer;
            background: #333;
            color: #fff;
        }

        button.primary {
            background: var(--accent);
            color: #000;
        }

        .chat {
            margin-top: 8px;
            background: rgba(0, 0, 0, 0.35);
            border-radius: 10px;
            padding: 8px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            height: 180px;
        }

        .chat .messages {
            flex: 1;
            overflow: auto;
            padding: 6px;
        }

        .chat .input {
            display: flex;
            gap: 6px;
        }

        .chat input {
            flex: 1;
            padding: 8px;
            border-radius: 8px;
            border: none;
            background: rgba(255, 255, 255, 0.03);
            color: #fff;
        }

        .small {
            font-size: 12px;
            opacity: 0.85;
        }
    </style>
</head>

<body>
    <div class="app-container">
        <aside class="sidebar">
            <h3 class="room-title">Room <span id="roomName"></span></h3>

            <div>
                <label class="small">Your language:</label>
                <div id="myLang" class="small" style="margin-bottom:10px;">-</div>
            </div>

            <h4 class="small">Participants</h4>
            <ul id="participants" class="participants-list"></ul>

            <div style="margin-top:12px;">
                <div class="chat">
                    <div id="chatMessages" class="messages"></div>
                    <div class="input">
                        <input id="chatInput" placeholder="Type a message..." />
                        <button id="sendChatBtn">Send</button>
                    </div>
                </div>
            </div>
        </aside>

        <main class="main">
            <div class="status" id="status">Initializing...</div>

            <div class="grid" id="tiles">
            </div>

            <div class="controls">
                <button id="joinBtn" class="primary">Join Call</button>
                <button id="muteBtn" disabled>🔇 Mute</button>
                <button id="unmuteBtn" disabled>🎙️ Unmute</button>
                <button id="leaveBtn">❌ Leave</button>
            </div>
        </main>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
    <script type="module">
        const BACKEND_URL = "{{BACKEND_URL}}"; 
        const FRONTEND_URL = {{FRONTEND_URL_JSON}};
        (async () => {
            const qs = new URLSearchParams(location.search);
            const ROOM = qs.get("room_code") || "";
            const USER = qs.get("user_id") || "";
            const MY_LANG = qs.get("lang") || ""; 
            document.getElementById('roomName').innerText = ROOM || "[unknown]";

            const TOKEN_ENDPOINT = BACKEND_URL + "/livekit/join-token";

            let lk;
            try {
                lk = await import('https://cdn.skypack.dev/livekit-client@^1.5.0');
            } catch (err) {
                console.error("Failed to load livekit-client from CDN", err);
                document.getElementById('status').innerText = "Failed to load LiveKit client. Check network.";
                return;
            }

            const { Room, RoomEvent, Track, createLocalTracks, DataPacket_Kind } = lk;

            let room = null;
            let localAudioTrack = null;
            let audioCtx = null;
            let joined = false;
            const participants = new Map(); 
            const tilesByIdentity = new Map(); 
            const preferredLanguage = MY_LANG || ''; 


            function addParticipantListEntry(id, name, isMe = false) {
                const ul = document.getElementById("participants");
                let li = ul.querySelector(`[data-id="${CSS.escape(id)}"]`);
                if (!li) {
                    li = document.createElement("li");
                    li.dataset.id = id;
                    li.innerHTML = `<span>${name}</span>${isMe ? '<span class="me-badge">You</span>' : ''}`;
                    ul.appendChild(li);
                } else {
                    li.querySelector("span").innerText = name;
                }
            }

            function removeParticipantListEntry(id) {
                const ul = document.getElementById("participants");
                const li = ul.querySelector(`[data-id="${CSS.escape(id)}"]`);
                if (li) li.remove();
            }

            function createTile(identity, displayName) {
                if (tilesByIdentity.has(identity)) return tilesByIdentity.get(identity);
                const div = document.createElement("div");
                div.className = "tile";
                div.id = "tile-" + btoa(identity).replace(/=/g, '');
                div.innerHTML = `
        <div class="avatar">🙂</div>
        <div class="username">${displayName || identity}</div>
        <div class="small" style="margin-top:8px;">lang: <span class="lang">-</span></div>
      `;
                document.getElementById("tiles").appendChild(div);
                tilesByIdentity.set(identity, div);
                return div;
            }

            function removeTile(identity) {
                const t = tilesByIdentity.get(identity);
                if (t) { t.remove(); tilesByIdentity.delete(identity); }
            }

            function setTileSpeaking(identity, speaking) {
                const t = tilesByIdentity.get(identity);
                if (!t) return;
                t.classList.toggle("speaking", !!speaking);
            }

            function setTileLanguage(identity, lang) {
                const t = tilesByIdentity.get(identity);
                if (!t) return;
                const el = t.querySelector(".lang");
                if (el) el.innerText = lang || "-";
            }

            function addChatMessage(from, text, me = false) {
                const box = document.getElementById("chatMessages");
                const d = document.createElement("div");
                d.className = "small";
                d.innerHTML = `<strong>${me ? "You" : from}:</strong> ${text}`;
                box.appendChild(d);
                box.scrollTop = box.scrollHeight;
            }

            function ensureAudioContext() {
                if (audioCtx == null) {
                    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                }
                if (audioCtx.state !== "running") {
                    const resume = () => {
                        audioCtx.resume().catch(() => { });
                        window.removeEventListener("click", resume);
                        window.removeEventListener("keydown", resume);
                    };
                    window.addEventListener("click", resume, { once: true });
                    window.addEventListener("keydown", resume, { once: true });
                }
            }

            function attachAudioTrack(track, identity) {
                let existing = document.getElementById("audio-" + btoa(identity).replace(/=/g, ''));
                if (existing) existing.remove();

                const audio = document.createElement("audio");
                audio.id = "audio-" + btoa(identity).replace(/=/g, '');
                audio.autoplay = true;
                audio.playsInline = true;
                audio.controls = false;
                audio.muted = false; 
                audio.style.display = "none";
                document.body.appendChild(audio);
                const el = track.attach();
                if (el.tagName && el.tagName.toLowerCase() === "audio") {
                    audio.srcObject = el.srcObject || el.src;
                } else {
                    audio.srcObject = track.mediaStreamTrack ? new MediaStream([track.mediaStreamTrack]) : null;
                }

                try {
                    ensureAudioContext();
                    const ctx = audioCtx;
                    const analyser = ctx.createAnalyser();
                    const src = ctx.createMediaElementSource(audio);
                    src.connect(analyser);
                    analyser.fftSize = 256;
                    const data = new Uint8Array(analyser.frequencyBinCount);
                    let raf;
                    const detect = () => {
                        analyser.getByteFrequencyData(data);
                        const v = data.reduce((a, b) => a + b, 0);
                        setTileSpeaking(identity, v > 50);
                        raf = requestAnimationFrame(detect);
                    };
                    detect();
                    audio.addEventListener("ended", () => { cancelAnimationFrame(raf); });
                } catch (e) {
                    console.warn("Audio analyser unavailable", e);
                }

                audio.play().catch(e => {
                    console.debug("autoplay blocked", e);
                });

                return audio;
            }

            async function joinCall() {
                if (!ROOM || !USER) {
                    alert("Missing room_code or user_id in URL query");
                    return;
                }
                document.getElementById('status').innerText = "Requesting token...";
                let tokenResp;
                try {
                    tokenResp = await fetch(TOKEN_ENDPOINT, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ room_code: ROOM, user_id: USER, name: USER, language: preferredLanguage })
                    });
                } catch (e) {
                    console.error("Token fetch failed", e);
                    document.getElementById('status').innerText = "Token request failed";
                    return;
                }
                if (!tokenResp.ok) {
                    const body = await tokenResp.text();
                    console.error("Token endpoint error", tokenResp.status, body);
                    document.getElementById('status').innerText = "Token request error";
                    return;
                }
                const { token, url: livekitUrl } = await tokenResp.json();
                document.getElementById('status').innerText = "Connecting to LiveKit...";

                try {
                    room = new Room();
                    await room.connect(livekitUrl, token, { name: USER });
                } catch (e) {
                    console.error("LiveKit connect failed", e);
                    document.getElementById('status').innerText = "LiveKit connect failed";
                    return;
                }

                document.getElementById('status').innerText = "Connected (LiveKit)";

                document.getElementById('myLang').innerText = preferredLanguage || "(unknown)";

                room.on(RoomEvent.ParticipantConnected, participant => {
                    console.log("participantConnected", participant.identity);
                    onParticipantConnected(participant);
                });
                room.on(RoomEvent.ParticipantDisconnected, participant => {
                    console.log("participantDisconnected", participant.identity);
                    onParticipantDisconnected(participant);
                });
                room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
                    const speakingIds = new Set(speakers.map(s => s.identity));
                    for (const id of tilesByIdentity.keys()) {
                        setTileSpeaking(id, speakingIds.has(id));
                    }
                });

                room.on(RoomEvent.DataReceived, (payload, participant) => {
                    try {
                        const text = new TextDecoder().decode(payload);
                        const parsed = JSON.parse(text);
                        if (parsed.type === "chat") {
                            addChatMessage(parsed.from, parsed.text, parsed.from === USER);
                        } else if (parsed.type === "subtitle") {
                            console.log("subtitle", parsed);
                        }
                    } catch (e) {
                        console.warn("Failed to parse data message", e);
                    }
                });

                try {
                    const tracks = await createLocalTracks({ audio: true });
                    if (tracks && tracks.length > 0) {
                        localAudioTrack = tracks.find(t => t.kind === Track.Kind.Audio);
                        await room.localParticipant.publishTrack(localAudioTrack);
                        document.getElementById('status').innerText = "Published local audio";
                        document.getElementById('muteBtn').disabled = false;
                        document.getElementById('unmuteBtn').disabled = false;
                    } else {
                        document.getElementById('status').innerText = "No local tracks available";
                    }
                } catch (e) {
                    console.error("Failed to create/publish local tracks", e);
                    document.getElementById('status').innerText = "Microphone access denied";
                }

                for (const p of room.participants.values()) {
                    onParticipantConnected(p);
                }

                joined = true;
            }

            async function onParticipantConnected(participant) {
                participants.set(participant.identity, participant);
                addParticipantListEntry(participant.identity, participant.name || participant.identity, participant.identity === USER);
                createTile(participant.identity, participant.name || participant.identity);

                if (participant.metadata) {
                    try {
                        const meta = JSON.parse(participant.metadata);
                        if (meta.language) setTileLanguage(participant.identity, meta.language);
                    } catch (e) {
                    }
                }

                participant.on(RoomEvent.TrackPublished, (publication) => {
                    
                    console.log("published", publication.trackSid, publication);
                });

                participant.on(RoomEvent.TrackSubscribed, (track, publication) => {
                    if (track.kind === Track.Kind.Audio) {
                        const langHint = (publication && publication.metadata) ? publication.metadata : null;
                        if (typeof langHint === 'string') {
                            try {
                                const parsed = JSON.parse(langHint);
                                if (parsed && parsed.lang) {
                                    setTileLanguage(participant.identity, parsed.lang);
                                }
                            } catch (e) {
                            }
                        }

                        attachAudioTrack(track, participant.identity);
                    }
                });

                for (const pub of participant.audioTracks.values()) {
                    if (pub.track) {
                        attachAudioTrack(pub.track, participant.identity);
                    } else {
                        participant.subscribe(pub).catch(() => { });
                    }
                }
            }

            function onParticipantDisconnected(participant) {
                participants.delete(participant.identity);
                removeParticipantListEntry(participant.identity);
                removeTile(participant.identity);
                
                const audioEl = document.getElementById("audio-" + btoa(participant.identity).replace(/=/g, ''));
                if (audioEl) audioEl.remove();
            }

            
            document.getElementById("joinBtn").addEventListener("click", async () => {
                
                ensureAudioContext();
                if (!joined) {
                    await joinCall();
                } else {
                    alert("Already joined");
                }
            });

            document.getElementById("muteBtn").addEventListener("click", () => {
                if (localAudioTrack) {
                    localAudioTrack.setMuted(true);
                    document.getElementById('status').innerText = "Muted";
                }
            });
            document.getElementById("unmuteBtn").addEventListener("click", () => {
                if (localAudioTrack) {
                    localAudioTrack.setMuted(false);
                    document.getElementById('status').innerText = "Unmuted";
                }
            });

            document.getElementById("leaveBtn").addEventListener("click", async () => {
                if (room) {
                    try { room.disconnect(); } catch (e) { }
                    room = null;
                    joined = false;
                    document.getElementById('status').innerText = "Left";
                }
                
                const redirect = (typeof FRONTEND_URL !== 'undefined') ? FRONTEND_URL : "/";
                
            });

            document.getElementById("sendChatBtn").addEventListener("click", async () => {
                const input = document.getElementById("chatInput");
                const text = input.value.trim();
                if (!text || !room) return;
                const payload = JSON.stringify({ type: "chat", from: USER, text });
                
                room.localParticipant.publishData(new TextEncoder().encode(payload), DataPacket_Kind.RELIABLE);
                addChatMessage(USER, text, true);
                input.value = "";
            });

            
            window.__lk_room = () => room;
            
            document.getElementById('status').innerText = "Ready — click Join Call to start";

            if (preferredLanguage) document.getElementById('myLang').innerText = preferredLanguage;
        })();
    </script>
</body>

</html>
"""

@app.get("/room", response_class=HTMLResponse)
async def room_page(room_code: str, user_id: str):
    room = rooms.get(room_code)
    if not room or user_id not in [m["user_id"] for m in room["members"]]:
        return HTMLResponse("<h2>Invalid room or user. Please (re)join from the app.</h2>", status_code=400)
    page = ROOM_HTML
    page = page.replace("{{BACKEND_URL}}", BACKEND_URL)
    page = page.replace("{{FRONTEND_URL_JSON}}", json_dumps(FRONTEND_URL))
    return HTMLResponse(page)


def json_dumps(x: str) -> str:
    import json as _json
    return _json.dumps(x)
