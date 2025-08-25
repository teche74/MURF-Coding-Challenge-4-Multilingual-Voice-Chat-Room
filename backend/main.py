import os, json, time, random, string, asyncio, logging
from typing import Dict, Optional, List
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from authlib.integrations.starlette_client import OAuth
from urllib.parse import quote
from dotenv import load_dotenv
from backend.bot_worker import ensure_room_bot, stop_room_bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(os.path.dirname(BASE_DIR), ".env")
load_dotenv(ENV)


CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://chatfree.streamlit.app")
BACKEND_URL = os.getenv("BACKEND_URL", "https://murf-coding-challenge-4-multilingual.onrender.com")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")

try:
    from livekit.api import AccessToken, VideoGrants
except Exception:
    AccessToken = None
    VideoGrants = None

if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL):
    logger.warning("LiveKit credentials not set. Set LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_URL in .env")

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



users: Dict[str, Dict] = {}
rooms: Dict[str, Dict] = {}
MAX_ROOM_CAPACITY = 4

# room shape: {
#   code: {
#       "public": bool,
#       "members": [{"user_id": str, "language": str}],
#       "bots": { lang_code: {"identity": str, "task": asyncio.Task} }
#   }
# }


app = FastAPI(title="LiveKit Translation Bots")
app.add_middleware(
    SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", "super-secret")
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _room_code(n: int = 6) -> str:
    import string

    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def _ensure_user(user_id: str, language: str = "en", voice: str = "default"):
    if user_id not in users:
        name = (user_id.split("@")[0] if "@" in user_id else user_id)[:32]
        users[user_id] = {"name": name, "language": language, "voice": voice}
    else:
        users[user_id]["language"] = language or users[user_id].get("language", "en")
        users[user_id]["voice"] = voice or users[user_id].get("voice", "default")

class CreateRoomRequest(BaseModel):
    user_id: str
    public: bool = True
    language: Optional[str] = "en"
    voice: Optional[str] = "default"


class JoinRoomRequest(BaseModel):
    user_id: str
    room_code: Optional[str] = None
    language: Optional[str] = "en"
    voice: Optional[str] = "default"


class LeaveRoomRequest(BaseModel):
    user_id: str
    room_code: str


class LiveKitJoinTokenReq(BaseModel):
    room_code: str
    user_id: str
    name: Optional[str] = None
    language: Optional[str] = "en"
    voice: Optional[str] = "default"


with open(os.path.join(BASE_DIR, "templates", "room.html"), "r", encoding="utf-8") as f:
    ROOM_HTML = f.read()


@app.get("/room", response_class=HTMLResponse)
def room_page(room_code: str, user_id: str, lang: Optional[str] = None):
    room = rooms.get(room_code)
    if not room or user_id not in [m["user_id"] for m in room["members"]]:
        return HTMLResponse(
            "<h2>Invalid room or user. Please (re)join from the app.</h2>",
            status_code=400,
        )
    page = ROOM_HTML.replace("{{BACKEND_URL}}", BACKEND_URL).replace(
        "{{FRONTEND_URL}}", FRONTEND_URL
    )
    return HTMLResponse(page)

@app.post("/create_room")
def create_room(req: CreateRoomRequest):
    _ensure_user(req.user_id, req.language, req.voice)
    code = _room_code()
    rooms[code] = {
        "public": req.public,
        "members": [{"user_id": req.user_id, "language": req.language or "en"}],
        "bots": {},
    }
    logger.info("room created %s by %s", code, req.user_id)
    return {"status": "success", "room_code": code}

@app.post("/join_room")
def join_room(req: JoinRoomRequest):
    _ensure_user(req.user_id, req.language, req.voice)
    if req.room_code:
        room = rooms.get(req.room_code)
        if not room:
            raise HTTPException(status_code=400, detail="Room not found")
        if len(room["members"]) >= MAX_ROOM_CAPACITY:
            raise HTTPException(status_code=400, detail="Room full")
        if not any(m["user_id"] == req.user_id for m in room["members"]):
            room["members"].append({"user_id": req.user_id, "language": req.language, "voice": req.voice})
        asyncio.create_task(_reconcile_bots(req.room_code))
        return {"status": "success", "room_code": req.room_code}

    public = [code for code, r in rooms.items() if r["public"] and len(r["members"]) < MAX_ROOM_CAPACITY]
    if not public:
        raise HTTPException(status_code=400, detail="No public rooms available")
    pick = random.choice(public)
    if not any(m["user_id"] == req.user_id for m in rooms[pick]["members"]):
        rooms[pick]["members"].append({"user_id": req.user_id, "language": req.language, "voice": req.voice})
    asyncio.create_task(_reconcile_bots(pick))
    return {"status": "success", "room_code": pick}

@app.post("/leave_room")
def leave_room(req: LeaveRoomRequest):
    room = rooms.get(req.room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    room["members"] = [m for m in room["members"] if m["user_id"] != req.user_id]
    logger.info("User %s left room %s", req.user_id, req.room_code)
    asyncio.create_task(_reconcile_bots(req.room_code))
    return {"status": "success"}


@app.get("/room_info")
def room_info(room_code: str):
    room = rooms.get(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return {"members": room["members"], "bots": list(room["bots"].keys())}


@app.get("/login/google")
async def login_google(request: Request):
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(
        request, redirect_uri, access_type="offline"
    )


@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    try:
        user_info = await oauth.google.parse_id_token(
            request, token, nonce=None, claims_options={"iss": {"essential": False}}
        )
    except Exception:
        user_info = await oauth.google.userinfo(token=token)

    user_email = user_info.get("email")
    if not user_email:
        raise HTTPException(status_code=400, detail="Email not found")

    users[user_email] = {"name": user_email.split("@")[0], "language": "en"}
    frontend_url = f"{FRONTEND_URL}/?user_id={quote(user_email)}&name={quote(users[user_email]['name'])}"
    return RedirectResponse(frontend_url)


@app.post("/livekit/join-token")
def livekit_join_token(req: LiveKitJoinTokenReq):
    if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL):
        raise HTTPException(500, "LiveKit not configured")
    if AccessToken is None or VideoGrants is None:
        raise HTTPException(500, "LiveKit server SDK missing")

    _ensure_user(req.user_id, req.language, req.voice)
    room = rooms.setdefault(req.room_code, {"public": True, "members": [], "bots": {}})
    found = next((m for m in room["members"] if m["user_id"] == req.user_id), None)
    if not found:
        room["members"].append({"user_id": req.user_id, "language": req.language, "voice": req.voice})
    else:
        found["language"] = req.language
        found["voice"] = req.voice

    asyncio.create_task(_reconcile_bots(req.room_code))

    try:
        meta = json.dumps({"language": req.language, "voice": req.voice})
        at = (
            AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(req.user_id)
            .with_name(req.name or req.user_id)
            .with_grants(VideoGrants(room_join=True, room=req.room_code, can_publish=True, can_subscribe=True, can_publish_data=True))
        )
        if hasattr(at, "with_metadata"):
            at = at.with_metadata(meta)
        token_jwt = at.to_jwt()
    except Exception as e:
        logger.exception("Failed to mint token")
        raise HTTPException(500, f"Token generation failed: {e}")

    return {"token": token_jwt, "url": LIVEKIT_URL, "room_code": req.room_code}

async def _reconcile_bots(room_code: str):
    room = rooms.get(room_code)
    if not room:
        return
    member_langs = [m.get("language", "en") for m in room["members"]]
    unique_langs = sorted(set(member_langs))

    if len(unique_langs) <= 1:
        await stop_room_bot(room_code)
        room["bots"].clear()
        return

    existing = set(room["bots"].keys())
    for lang in unique_langs:
        if lang not in existing:
            bot = await ensure_room_bot(room_code, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            room["bots"][lang] = {"identity": f"bot_{room_code}", "task": bot}
    for lang in list(existing):
        if lang not in unique_langs:
            t = room["bots"].pop(lang, None)
            if t and t.get("task"):
                t["task"].cancel()


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

        #status {
            margin-top: 10px;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 500;
            background-color: #f3f4f6;
            /* light gray */
            color: #333;
            display: inline-block;
            transition: all 0.3s ease-in-out;
        }



        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            justify-items: center;
        }

        .tile video {
            width: 100%;
            height: 120px;
            object-fit: cover;
            border-radius: 8px;
            margin-top: 6px;
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
            width: 50px;
            /* adjust size */
            height: 50px;
            border-radius: 50%;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #e0e0e0;
            /* fallback background */
        }

        .avatar img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .controls {
            display: flex;
            gap: 12px;
            justify-content: center;
            margin-top: 6px;
        }

        button {
            margin: 8px 6px;
            padding: 10px 18px;
            font-size: 14px;
            font-weight: 600;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: transform 0.2s ease, background-color 0.3s ease;
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

        #muteBtn {
            background-color: #ef4444;
            color: white;
        }

        #muteBtn:hover {
            background-color: #dc2626;
            transform: scale(1.05);
        }

        #muteBtn:disabled {
            background-color: #fca5a5;
            cursor: not-allowed;
        }

        #unmuteBtn {
            background-color: #22c55e;
            color: white;
        }

        #unmuteBtn:hover {
            background-color: #16a34a;
            transform: scale(1.05);
        }

        #unmuteBtn:disabled {
            background-color: #86efac;
            cursor: not-allowed;
        }

        #video-container {
            margin-top: 15px;
            border: 2px solid #e5e7eb;
            border-radius: 10px;
            overflow: hidden;
            max-width: 400px;
        }

        #video-container video {
            width: 100%;
            height: auto;
            border-radius: 10px;
        }

        .tile .video-slot video {
            width: 100%;
            height: 120px;
            border-radius: 8px;
            object-fit: cover;
            margin-top: 6px;
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


            <div id="localVideoContainer" class="tile" style="max-width:220px; margin:0 auto; display:none;">
                <div class="small">Your Video</div>
            </div>

            <div class="grid" id="tiles">
            </div>

            <div class="controls">
                <button id="joinBtn" class="primary">Join Call</button>
                <button id="muteBtn" disabled>🔇 Mute</button>
                <button id="unmuteBtn" disabled>🎙️ Unmute</button>
                <button id="leaveBtn">❌ Leave</button>
            </div>

            <div class="controls">
                <label style="display:flex;align-items:center;gap:6px;">
                    <input type="checkbox" id="useVideo" />
                    🎥 Use video
                </label>
            </div>
        </main>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
    <script type="module">
        const BACKEND_URL = "{{BACKEND_URL}}";
        const FRONTEND_URL = "{{FRONTEND_URL}}";
        (async () => {
            const qs = new URLSearchParams(location.search);
            const ROOM = qs.get("room_code") || "";
            const USER = qs.get("user_id") || "";
            const MY_LANG = qs.get("lang") || "";
            document.getElementById('roomName').innerText = ROOM || "[unknown]";

            const TOKEN_ENDPOINT = BACKEND_URL + "/livekit/join-token";
            console.log("ENDPOINT : ", TOKEN_ENDPOINT)
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
            let localVideoTrack = null;
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
        <div class="avatar">
            <img src="data:image/png;base64,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" alt="Default Avatar">
        </div>
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


                try { startTranslateStream(track, identity); } catch (e) { console.warn("stream err", e); }

                return audio;
            }

            const WS_URL = (BACKEND_URL.startsWith("https://") ? "wss://" + BACKEND_URL.slice(8)
                : BACKEND_URL.startsWith("http://") ? "ws://" + BACKEND_URL.slice(7)
                    : BACKEND_URL) + "/ws/translate";

            let translateSockets = new Map();
            let pcmCtx;

            function ensurePCMContext() {
                if (!pcmCtx) {
                    pcmCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
                }
            }

            function floatTo16bitPCM(float32Array) {
                const out = new Int16Array(float32Array.length);
                for (let i = 0; i < float32Array.length; i++) {
                    let s = Math.max(-1, Math.min(1, float32Array[i]));
                    out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                }
                return out;
            }

            async function ensureWorklet(ctx) {
                if (!ctx.audioWorklet) return false;
                const code = `
                    class PCMWriter extends AudioWorkletProcessor {
                        process(inputs) {
                        const input = inputs[0];
                        if (input && input[0]) {
                            this.port.postMessage(input[0]);
                        }
                        return true;
                        }
                    }
                    registerProcessor('pcm-writer', PCMWriter);
        `;
                const blob = new Blob([code], { type: 'application/javascript' });
                const url = URL.createObjectURL(blob);
                await ctx.audioWorklet.addModule(url);
                return true;
            }

            async function startTranslateStream(track, speakerId) {
                ensurePCMContext();
                const ctx = pcmCtx;
                const stream = new MediaStream([track.mediaStreamTrack]);
                const src = ctx.createMediaStreamSource(stream);

                await ensureWorklet(ctx);
                const worklet = new AudioWorkletNode(ctx, 'pcm-writer');
                src.connect(worklet);

                const ws = new WebSocket(WS_URL);
                ws.binaryType = "arraybuffer";
                translateSockets.set(speakerId, ws);

                ws.addEventListener("open", () => {
                    ws.send(JSON.stringify({
                        type: "start",
                        speakerId,
                        fromLang: "auto",
                        targetLang: preferredLanguage || "en",
                        voice: "en-US-Wavenet-D"
                    }));
                });

                const translatedAudio = new Audio();
                translatedAudio.autoplay = true;
                translatedAudio.playsInline = true;
                let queue = [];
                let playing = false;
                function playNext() {
                    if (playing || queue.length === 0) return;
                    playing = true;
                    const { mime, bytes } = queue.shift();
                    const blob = new Blob([bytes], { type: mime || "audio/mpeg" });
                    translatedAudio.src = URL.createObjectURL(blob);
                    translatedAudio.onended = () => { playing = false; playNext(); };
                    translatedAudio.onerror = () => { playing = false; playNext(); };
                    translatedAudio.play().catch(() => { playing = false; });
                }

                ws.addEventListener("message", (ev) => {
                    try {
                        const data = JSON.parse(ev.data);
                        if (data.type === "audio" && data.b64) {
                            const bytes = Uint8Array.from(atob(data.b64), c => c.charCodeAt(0));
                            queue.push({ mime: data.mime, bytes });
                            playNext();
                        }
                        // (optional: handle partialText/finalText for captions)
                    } catch { }
                });

                worklet.port.onmessage = (e) => {
                    const floatFrame = e.data;
                    const pcm16 = floatTo16bitPCM(floatFrame);
                    if (ws.readyState === WebSocket.OPEN) {
                        ws.send(pcm16.buffer);
                    }
                };

                room.on(RoomEvent.ParticipantDisconnected, (p) => {
                    if (p.identity === speakerId) {
                        try { ws.send(JSON.stringify({ type: "stop" })); } catch { }
                        try { ws.close(); } catch { }
                        translateSockets.delete(speakerId);
                    }
                });
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
                    console.log("livekit url : ", livekitUrl)
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
                    const useVideo = document.getElementById('useVideo').checked;
                    const tracks = await createLocalTracks({ audio: true, video: useVideo ? true : false });

                    if (tracks && tracks.length > 0) {
                        localAudioTrack = tracks.find(t => t.kind === Track.Kind.Audio);
                        localVideoTrack = tracks.find(t => t.kind === Track.Kind.Video);

                        if (localAudioTrack) {
                            await room.localParticipant.publishTrack(localAudioTrack);
                            document.getElementById('muteBtn').disabled = false;
                            document.getElementById('unmuteBtn').disabled = false;
                        }

                        if (useVideo && localVideoTrack) {
                            await room.localParticipant.publishTrack(localVideoTrack);

                            const videoEl = document.createElement('video');
                            videoEl.autoplay = true;
                            videoEl.playsInline = true;
                            videoEl.muted = true;
                            localVideoTrack.attach(videoEl);

                            const container = document.getElementById('localVideoContainer');
                            container.style.display = "block";
                            container.appendChild(videoEl);
                        }

                        document.getElementById('status').innerText = useVideo ? "Published local audio & video" : "Published local audio only";
                    } else {
                        document.getElementById('status').innerText = "No local tracks available";
                    }
                } catch (e) {
                    console.error("Failed to create/publish local tracks", e);
                    document.getElementById('status').innerText = "Microphone/Camera access denied";
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
                    if (track.kind === Track.Kind.Video) {
                        const tile = tilesByIdentity.get(participant.identity);
                        if (tile) {
                            let videoEl = tile.querySelector("video");
                            if (!videoEl) {
                                videoEl = document.createElement("video");
                                videoEl.autoplay = true;
                                videoEl.playsInline = true;
                                tile.appendChild(videoEl);
                            }
                            track.attach(videoEl);
                        }
                    }
                });

                for (const pub of participant.tracks.values()) {
                    if (pub.track) {
                        if (pub.track.kind === Track.Kind.Audio) {
                            attachAudioTrack(pub.track, participant.identity);
                        }
                        if (pub.track.kind === Track.Kind.Video) {
                            const tile = tilesByIdentity.get(participant.identity);
                            if (tile) {
                                let videoWrap = tile.querySelector(".video-slot");
                                if (!videoWrap) {
                                    videoWrap = document.createElement("div");
                                    videoWrap.className = "video-slot";
                                    tile.appendChild(videoWrap);
                                }
                                let videoEl = videoWrap.querySelector("video");
                                if (!videoEl) {
                                    videoEl = document.createElement("video");
                                    videoEl.autoplay = true;
                                    videoEl.playsInline = true;
                                    videoWrap.appendChild(videoEl);
                                }
                                track.attach(videoEl);
                            }
                        }
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