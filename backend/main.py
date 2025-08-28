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
#       "bot": Optional[object]
#   }
# }


app = FastAPI(title="LiveKit Translation bot")
app.add_middleware(
    SessionMiddleware, 
    secret_key=os.getenv("SESSION_SECRET_KEY", "super-secret"),
    same_site="none",
    https_only=True,
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


@app.get("/room", response_class=HTMLResponse)
def room_page(room_code: str, user_id: str, lang: Optional[str] = None):
    logger.info(f"GET /room called with room_code={room_code}, user_id={user_id}, lang={lang}")
    room = rooms.get(room_code)
    if not room or user_id not in [m["user_id"] for m in room["members"]]:
        logger.warning(f"Invalid room or user: room_code={room_code}, user_id={user_id}")
        return HTMLResponse(
            "<h2>Invalid room or user. Please (re)join from the app.</h2>",
            status_code=400,
        )
    page = ROOM_HTML.replace("{{BACKEND_URL}}", BACKEND_URL).replace(
        "{{FRONTEND_URL}}", FRONTEND_URL
    )
    logger.info(f"Room page served for room_code={room_code}, user_id={user_id}")
    return HTMLResponse(page)

@app.post("/create_room")
def create_room(req: CreateRoomRequest):
    logger.info(f"POST /create_room called by user_id={req.user_id}, public={req.public}, language={req.language}, voice={req.voice}")
    _ensure_user(req.user_id, req.language, req.voice)
    code = _room_code()
    rooms[code] = {
        "public": req.public,
        "members": [{"user_id": req.user_id, "language": req.language or "en"}],
        "bot": None,
    }
    logger.info(f"Room created: code={code}, by user_id={req.user_id}")
    return {"status": "success", "room_code": code}

@app.post("/join_room")
async def join_room(req: JoinRoomRequest):
    logger.info(f"POST /join_room called by user_id={req.user_id}, room_code={req.room_code}, language={req.language}, voice={req.voice}")
    _ensure_user(req.user_id, req.language, req.voice)

    if req.room_code:
        room = rooms.get(req.room_code)
        if not room:
            logger.error(f"Room not found: room_code={req.room_code}")
            raise HTTPException(status_code=400, detail="Room not found")
        if len(room["members"]) >= MAX_ROOM_CAPACITY:
            logger.warning(f"Room full: room_code={req.room_code}")
            raise HTTPException(status_code=400, detail="Room full")

        if not any(m["user_id"] == req.user_id for m in room["members"]):
            room["members"].append({
                "user_id": req.user_id,
                "language": req.language,
                "voice": req.voice
            })
            logger.info(f"User {req.user_id} joined room {req.room_code}")

        bot = await ensure_room_bot(req.room_code, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        await bot.set_user_pref(req.user_id, req.language, req.voice)
        logger.info(f"bot ensured for room {req.room_code}")

        return {"status": "success", "room_code": req.room_code}

    public = [code for code, r in rooms.items() if r["public"] and len(r["members"]) < MAX_ROOM_CAPACITY]
    if not public:
        logger.warning("No public rooms available")
        raise HTTPException(status_code=400, detail="No public rooms available")

    pick = random.choice(public)
    logger.info(f"User {req.user_id} joining random public room {pick}")

    if not any(m["user_id"] == req.user_id for m in rooms[pick]["members"]):
        rooms[pick]["members"].append({
            "user_id": req.user_id,
            "language": req.language,
            "voice": req.voice
        })
        logger.info(f"User {req.user_id} added to room {pick}")

    bot = await ensure_room_bot(pick, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    await bot.set_user_pref(req.user_id, req.language, req.voice)
    logger.info(f"bot ensured for room {pick}")

    return {"status": "success", "room_code": pick}

@app.post("/leave_room")
async def leave_room(req: LeaveRoomRequest):
    logger.info(f"POST /leave_room called by user_id={req.user_id}, room_code={req.room_code}")
    room = rooms.get(req.room_code)
    if not room:
        logger.error(f"Room not found: room_code={req.room_code}")
        raise HTTPException(status_code=404, detail="Room not found")
    room["members"] = [m for m in room["members"] if m["user_id"] != req.user_id]
    logger.info(f"User {req.user_id} left room {req.room_code}")
    asyncio.create_task(_reconcile_bots(req.room_code))
    return {"status": "success"}


@app.get("/room_info")
def room_info(room_code: str):
    logger.info(f"GET /room_info called for room_code={room_code}")
    room = rooms.get(room_code)
    if not room:
        logger.error(f"Room not found: room_code={room_code}")
        raise HTTPException(status_code=404, detail="Room not found")
    logger.info(f"Room info served for room_code={room_code}")
    return {"members": room["members"], "bot": bool(room["bot"])}


@app.get("/login/google")
async def login_google(request: Request):
    logger.info("GET /login/google called")
    redirect_uri = request.url_for("auth_callback")
    logger.debug("session before authorize_redirect: keys=%s", list(request.session.keys()))
    return await oauth.google.authorize_redirect(
        request, redirect_uri, access_type="offline"
    )


@app.get("/auth/callback")
async def auth_callback(request: Request):
    logger.info("GET /auth/callback called")
    logger.info("GET /auth/callback called")
    logger.debug("session on callback: keys=%s", list(request.session.keys()))
    token = await oauth.google.authorize_access_token(request)
    try:
        user_info = await oauth.google.parse_id_token(
            request, token, nonce=None, claims_options={"iss": {"essential": False}}
        )
        logger.info("Google ID token parsed successfully")
    except Exception:
        user_info = await oauth.google.userinfo(token=token)
        logger.warning("Google ID token parse failed, fallback to userinfo")

    user_email = user_info.get("email")
    if not user_email:
        logger.error("Email not found in user_info")
        raise HTTPException(status_code=400, detail="Email not found")

    users[user_email] = {"name": user_email.split("@")[0], "language": "en"}
    logger.info(f"User logged in: {user_email}")
    frontend_url = f"{FRONTEND_URL}/?user_id={quote(user_email)}&name={quote(users[user_email]['name'])}"
    return RedirectResponse(frontend_url)


@app.post("/livekit/join-token")
async def livekit_join_token(req: LiveKitJoinTokenReq):
    logger.info(f"POST /livekit/join-token called for room_code={req.room_code}, user_id={req.user_id}, language={req.language}, voice={req.voice}")
    if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL):
        logger.error("LiveKit not configured")
        raise HTTPException(500, "LiveKit not configured")
    if AccessToken is None or VideoGrants is None:
        logger.error("LiveKit server SDK missing")
        raise HTTPException(500, "LiveKit server SDK missing")

    _ensure_user(req.user_id, req.language, req.voice)
    room = rooms.setdefault(req.room_code, {"public": True, "members": [], "bot": None})
    found = next((m for m in room["members"] if m["user_id"] == req.user_id), None)
    if not found:
        room["members"].append({"user_id": req.user_id, "language": req.language, "voice": req.voice})
        logger.info(f"User {req.user_id} added to room {req.room_code}")
    else:
        found["language"] = req.language
        found["voice"] = req.voice
        logger.info(f"User {req.user_id} preferences updated in room {req.room_code}")

    asyncio.create_task(_reconcile_bots(req.room_code))

    try:
        meta = json.dumps({"language": req.language, "voice": req.voice})
        at = (
            AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(req.user_id)
            .with_name(req.name or req.user_id)
            .with_grants(VideoGrants(
                room_join=True,
                room=req.room_code,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True
            ))
        )
        if hasattr(at, "with_metadata"):
            at = at.with_metadata(meta)
        token_jwt = at.to_jwt()
        logger.info(f"LiveKit token minted for user_id={req.user_id}, room_code={req.room_code}")
    except Exception as e:
        logger.exception("Failed to mint token")
        raise HTTPException(500, f"Token generation failed: {e}")

    return {"token": token_jwt, "url": LIVEKIT_URL, "room_code": req.room_code}

async def _reconcile_bots(room_code: str):
    room = rooms.get(room_code)
    if not room:
        return

    if len(room["members"]) <= 1:
        if room.get("bot"):
            await stop_room_bot(room_code)
            room["bot"] = None
        return

    if not room.get("bot"):
        bot = await ensure_room_bot(room_code, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        room["bot"] = bot

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

            <div class="grid" id="tiles"></div>

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
            let lk;
            try {
                lk = await import('https://cdn.skypack.dev/livekit-client@^1.5.0');
            } catch (err) {
                console.error("Failed to load livekit-client", err);
                document.getElementById('status').innerText = "Failed to load LiveKit client.";
                return;
            }

            const { Room, RoomEvent, Track, createLocalTracks, DataPacket_Kind } = lk;

            let room = null;
            let localAudioTrack = null;
            let localVideoTrack = null;
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
            <img src="data:image/jpeg;base64,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" alt="Avatar">
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

                track.attach(audio);

                const tryPlay = () => {
                    audio.play().catch(() => {
                        showAudioNudge();
                    });
                };
                tryPlay();

                if (identity.startsWith("bot_")) {
                    audio.addEventListener("play", () => {
                        document.querySelectorAll('audio[id^="audio-"]').forEach(a => {
                            if (a.id !== audio.id) a.muted = true;
                        });
                    });
                    audio.addEventListener("ended", () => {
                        document.querySelectorAll('audio[id^="audio-"]').forEach(a => {
                            if (a.id !== audio.id) a.muted = false;
                        });
                    });
                    audio.addEventListener("pause", () => {
                        document.querySelectorAll('audio[id^="audio-"]').forEach(a => {
                            if (a.id !== audio.id) a.muted = false;
                        });
                    });
                }

                return audio;
            }

            let audioNudgeShown = false;
            function showAudioNudge() {
                if (audioNudgeShown) return;
                audioNudgeShown = true;
                const btn = document.createElement('button');
                btn.textContent = "🔊 Tap to enable audio";
                btn.style = "position:fixed;bottom:12px;left:12px;z-index:9999;padding:6px 12px;";
                btn.onclick = () => {
                    document.querySelectorAll('audio').forEach(a => a.play().catch(() => { }));
                    if (window.AudioContext) {
                        try {
                            const ac = new AudioContext();
                            if (ac.state === "suspended") ac.resume();
                        } catch { }
                    }
                    btn.remove();
                };
                document.body.appendChild(btn);
            }



            async function joinCall() {
                if (!ROOM || !USER) {
                    alert("Missing room_code or user_id");
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
                    console.error("Token error", body);
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

                // participant handlers
                room.on(RoomEvent.ParticipantConnected, onParticipantConnected);
                room.on(RoomEvent.ParticipantDisconnected, onParticipantDisconnected);

                room.on(RoomEvent.DataReceived, (payload, participant) => {
                    try {
                        const parsed = JSON.parse(new TextDecoder().decode(payload));
                        if (parsed.type === "chat") {
                            addChatMessage(parsed.from, parsed.text, parsed.from === USER);
                        }
                    } catch (e) {
                        console.warn("Failed to parse data message", e);
                    }
                });

                try {
                    const useVideo = document.getElementById('useVideo').checked;
                    const tracks = await createLocalTracks({ audio: true, video: useVideo });
                    localAudioTrack = tracks.find(t => t.kind === Track.Kind.Audio);
                    localVideoTrack = tracks.find(t => t.kind === Track.Kind.Video);

                    if (localAudioTrack) {
                        localAudioTrack.on("volume", (vol) => {
                            console.log("Mic volume:", vol);
                        });

                    // Attach locally so you can hear yourself
                        const testAudio = document.createElement("audio");
                        testAudio.autoplay = true;
                        testAudio.playsInline = true;
                        testAudio.muted = true; // prevent feedback
                        localAudioTrack.attach(testAudio);
                        document.body.appendChild(testAudio);

                        await room.localParticipant.publishTrack(localAudioTrack);
                        document.getElementById('muteBtn').disabled = false;
                        document.getElementById('unmuteBtn').disabled = false;
                    }
                    if (localVideoTrack) {
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
                } catch (e) {
                    console.error("Track publish failed", e);
                }

                for (const p of room.participants.values()) {
                    onParticipantConnected(p);
                }
                joined = true;
            }

            function onParticipantConnected(participant) {
                participants.set(participant.identity, participant);
                addParticipantListEntry(participant.identity, participant.name || participant.identity, participant.identity === USER);
                createTile(participant.identity, participant.name || participant.identity);

                try {
                    for (const pub of participant.audioTracks.values()) {
                        if (pub && pub.isSubscribed && pub.track) {
                            attachAudioTrack(pub.track, participant.identity);
                        }
                    }
                    for (const pub of participant.videoTracks.values()) {
                        if (pub && pub.isSubscribed && pub.track) {
                            const tile = tilesByIdentity.get(participant.identity);
                            if (tile) {
                                let videoEl = tile.querySelector("video");
                                if (!videoEl) {
                                    videoEl = document.createElement("video");
                                    videoEl.autoplay = true;
                                    videoEl.playsInline = true;
                                    tile.appendChild(videoEl);
                                }
                                pub.track.attach(videoEl);
                            }
                        }
                    }
                } catch (e) {
                    console.warn("Attach existing tracks failed", e);
                }

                participant.on(RoomEvent.TrackSubscribed, (track, publication) => {
                    if (track.kind === Track.Kind.Audio) {
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
            }


            function onParticipantDisconnected(participant) {
                participants.delete(participant.identity);
                removeParticipantListEntry(participant.identity);
                removeTile(participant.identity);
            }

            // Buttons
            document.getElementById("joinBtn").addEventListener("click", joinCall);
            document.getElementById("muteBtn").addEventListener("click", () => {
                if (localAudioTrack) localAudioTrack.setMuted(true);
            });
            document.getElementById("unmuteBtn").addEventListener("click", () => {
                if (localAudioTrack) localAudioTrack.setMuted(false);
            });
            document.getElementById("leaveBtn").addEventListener("click", () => {
                if (room) room.disconnect();
                room = null;
                joined = false;
                document.getElementById('status').innerText = "Left";
            });

            document.getElementById("sendChatBtn").addEventListener("click", () => {
                const input = document.getElementById("chatInput");
                const text = input.value.trim();
                if (!text || !room) return;
                const payload = JSON.stringify({ type: "chat", from: USER, text });
                room.localParticipant.publishData(new TextEncoder().encode(payload), DataPacket_Kind.RELIABLE);
                addChatMessage(USER, text, true);
                input.value = "";
            });

            document.getElementById('status').innerText = "Ready — click Join Call to start";
            if (preferredLanguage) document.getElementById('myLang').innerText = preferredLanguage;
        })();
    </script>
</body>

</html>
"""
