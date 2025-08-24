import sys, os, time, random, string, logging, asyncio, base64
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from typing import Dict, Optional, Tuple
import json

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from authlib.integrations.starlette_client import OAuth
from urllib.parse import unquote, quote

from dotenv import load_dotenv

from backend.speech_to_text_and_translation_utils import speech_to_text, translate_text
from backend.murf_api import generate_speech_from_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- env ---
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(env_path)

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://chatfree.streamlit.app")

# --- oauth ---
oauth = OAuth()
oauth.register(
    name='google',
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    jwks_uri='https://www.googleapis.com/oauth2/v3/certs',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'email openid'}
)

# --- in-memory stores (consider Redis later) ---
users: Dict[str, Dict] = {}
rooms: Dict[str, Dict] = {}
MAX_ROOM_CAPACITY = 4

# TTS cache
TTS_CACHE: Dict[Tuple[str, str, str], Tuple[bytes, float]] = {}
CACHE_TTL_SECONDS = 60 * 5

# --- app ---
app = FastAPI(title="Multilingual Chat Room")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", "super-secret"))
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# --- helpers ---
def generate_room_code(length: int = 6) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def get_user_name(user_id: str) -> str:
    return users.get(user_id, {}).get("name", user_id)

def ensure_user(user_id: str):
    """Best-effort auto-upsert so restarts don't break room join/create."""
    if user_id not in users:
        name = (user_id.split("@")[0] if "@" in user_id else user_id)[:32]
        users[user_id] = {"name": name, "language": "en"}

async def synthesize_with_cache(text: str, target_lang: str, voice: str="default"):
    key = (text, target_lang, voice)
    now = time.time()
    entry = TTS_CACHE.get(key)
    if entry and now - entry[1] < CACHE_TTL_SECONDS:
        return entry[0]
    audio_bytes = await asyncio.to_thread(generate_speech_from_text, text, target_lang, voice)
    TTS_CACHE[key] = (audio_bytes, now)
    return audio_bytes

# --- models ---
class CreateRoomRequest(BaseModel):
    user_id: str
    public: bool = True

class JoinRoomRequest(BaseModel):
    user_id: str
    room_code: Optional[str] = None

class LeaveRoomRequest(BaseModel):
    user_id: str
    room_code: str

# --- basic APIs ---
@app.get("/room_info")
def room_info(room_code: str):
    room = rooms.get(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return {"members": room["members"]}

@app.post("/create_room")
def create_room(req: CreateRoomRequest):
    ensure_user(req.user_id)
    room_code = generate_room_code()
    rooms[room_code] = {"members": [req.user_id], "public": req.public}
    logger.info(f"room created {room_code} by {req.user_id}")
    return {"status": "success", "room_code": room_code}

@app.post("/join_room")
def join_room(req: JoinRoomRequest):
    ensure_user(req.user_id)

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
        if req.user_id not in rooms[selected_code]["members"]:
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

# --- auth ---
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

# --- STT/TTS ---
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

# --- WebSocket room manager ---
class WSRoomManager:
    def __init__(self):
        self.rooms_ws: Dict[str, Dict[str, WebSocket]] = {}
        self._recent_signals: Dict[Tuple, float] = {}
        self._dedupe_ttl = 2.0 

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

    async def relay(self, room_code: str, from_user: str, target_user: str, payload: dict):
        """
        Relay a signaling payload to a specific target_user.
        - Do not send to self.
        - Dedupe identical payloads for a short window to avoid duplicates being re-applied.
        """
        if from_user == target_user:
            logger.warning(f"Skipping relay: sender==target ({from_user})")
            return

        target_ws = self.rooms_ws.get(room_code, {}).get(target_user)
        if not target_ws:
            logger.warning(f"Relay target not connected: {target_user} in {room_code}")
            return

        try:
            data_serialized = json.dumps(payload.get("data", ""), sort_keys=True)
        except Exception:
            data_serialized = str(payload.get("data", ""))

        key = (room_code, from_user, target_user, payload.get("type"), data_serialized)
        now = time.time()
        last_ts = self._recent_signals.get(key)
        if last_ts and (now - last_ts) < self._dedupe_ttl:
            logger.info("Dropping duplicate signaling msg (within TTL) %s -> %s type=%s", from_user, target_user, payload.get("type"))
            return

        self._recent_signals[key] = now
        for k, ts in list(self._recent_signals.items()):
            if now - ts > self._dedupe_ttl:
                self._recent_signals.pop(k, None)

        try:
            await target_ws.send_json(payload)
        except Exception as e:
            logger.warning("Failed to relay to %s: %s", target_user, e)

ws_manager = WSRoomManager()

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    room_code = unquote(websocket.query_params.get("room_code", ""))
    user_id = unquote(websocket.query_params.get("user_id", ""))

    if not room_code or not user_id:
        await websocket.close()
        return

    room = rooms.get(room_code)
    if not room or user_id not in room["members"]:
        await websocket.close()
        return

    await ws_manager.connect(room_code, user_id, websocket)

    existing_peers = list((await ws_manager.peers_in_room(room_code, exclude=user_id)).keys())
    peers_payload = [{"user_id": uid, "name": get_user_name(uid)} for uid in existing_peers]
    await ws_manager.send_json(websocket, {"type": "peers", "peers": peers_payload})

    for peer_ws in (await ws_manager.peers_in_room(room_code, exclude=user_id)).values():
        try:
            await ws_manager.send_json(peer_ws, {
                "type": "peer-joined",
                "user_id": user_id,
                "name": get_user_name(user_id)
            })
        except Exception:
            pass

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            to_user = msg.get("to")
            data = msg.get("data", {})

            if mtype in ("offer", "answer", "ice-candidate") and to_user:
                await ws_manager.relay(room_code, user_id, to_user, {
                "type": mtype,
                "from": user_id,
                "name": get_user_name(user_id),
                "data": data
            })
            elif mtype == "chat":
                text = msg.get("text", "")
                broadcast_payload = {"type": "chat", "from": user_id, "name": get_user_name(user_id), "text": text}
                for peer_ws in (await ws_manager.peers_in_room(room_code, exclude="")).values():
                    try:
                        await ws_manager.send_json(peer_ws, broadcast_payload)
                    except Exception:
                        pass

    except WebSocketDisconnect:
        logger.info(f"Websocket disconnect for {user_id}")
    except Exception as e:
        logger.exception("ws error: %s", e)
    finally:
        for peer_ws in (await ws_manager.peers_in_room(room_code, exclude=user_id)).values():
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

# --- NEW: serve the room UI outside Streamlit (full mic permission) ---
ROOM_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Voice Room</title>
    <style>
        /* (styles unchanged — omitted for brevity here in the snippet but keep your full CSS) */
        body { margin: 0; font-family: 'Segoe UI', sans-serif; background: linear-gradient(135deg, #0f2027, #203a43, #2c5364); color: #eee; display: flex; height: 100vh; }
        /* rest of your CSS... */
    </style>
</head>
<body>
    <div class="app-container">
        <aside class="sidebar">
            <div>
                <h2 class="room-title">Room: <span id="roomName"></span></h2>
                <ul id="participants" class="participants-list"></ul>
            </div>
            <div class="chat-box">
                <div id="chat-messages" class="messages"></div>
                <div class="chat-input">
                    <input type="text" id="chatInput" placeholder="Type a message..." />
                    <button id="sendBtn">➤</button>
                </div>
            </div>
        </aside>
        <main class="main-content">
            <div id="status" class="status-bar">Connecting...</div>
            <div class="user-grid">
                <div class="user-card empty" id="user1">
                    <div class="avatar">🙂</div>
                    <div class="username">Empty</div>
                </div>
                <div class="user-card empty" id="user2">
                    <div class="avatar">🙂</div>
                    <div class="username">Empty</div>
                </div>
                <div class="user-card empty" id="user3">
                    <div class="avatar">🙂</div>
                    <div class="username">Empty</div>
                </div>
                <div class="user-card empty" id="user4">
                    <div class="avatar">🙂</div>
                    <div class="username">Empty</div>
                </div>
            </div>
            <div class="controls">
                <button id="muteBtn" class="control-btn" disabled>🔇 Mute</button>
                <button id="unmuteBtn" class="control-btn" disabled>🎙️ Unmute</button>
                <button id="leaveBtn" class="control-btn leave">❌ Leave</button>
            </div>
        </main>
    </div>

    <script>
        /***** Config *****/
        const qs = new URLSearchParams(location.search);
        const ROOM = qs.get("room_code") || "";
        const USER = qs.get("user_id") || "";
        const BACKEND_HTTP = location.origin;
        const WS_URL = BACKEND_HTTP.replace(/^http/i, "ws") + "/ws?room_code=" + encodeURIComponent(ROOM) + "&user_id=" + encodeURIComponent(USER);
        const FRONTEND_URL = {{ FRONTEND_URL_JSON }};

        console.log("WS_URL", WS_URL, "HTTP", BACKEND_HTTP, "ROOM", ROOM, "USER", USER);
        document.getElementById('roomName').innerText = ROOM;

        /***** State *****/
        let localStream = null;
        let ws = null;
        const peers = new Map(); // peerId -> RTCPeerConnection
        const userSlots = ["user1", "user2", "user3", "user4"];
        const peerSlotMap = new Map();
        const remoteDescReady = new Map();       // peerId -> bool
        const participants = new Set();
        const pendingCandidates = new Map();     // peerId -> [ICE candidates]

        /***** Helpers UI *****/
        function updateParticipantsUI() {
            const list = document.getElementById("participants");
            list.innerHTML = "";
            for (let uid of participants) {
                const li = document.createElement("li");
                const you = uid === USER ? '<span class="me-badge">You</span>' : '';
                li.innerHTML = `<span>${uid}</span>${you}`;
                list.appendChild(li);
            }
        }
        function assignSlot(userId, label) {
            if (peerSlotMap.has(userId)) return peerSlotMap.get(userId);
            for (let slot of userSlots) {
                const el = document.getElementById(slot);
                if (el.classList.contains("empty")) {
                    el.classList.remove("empty");
                    el.querySelector(".username").innerText = label || userId;
                    peerSlotMap.set(userId, slot);
                    participants.add(userId);
                    updateParticipantsUI();
                    return slot;
                }
            }
            return null;
        }
        function removeSlot(userId) {
            const slot = peerSlotMap.get(userId);
            if (!slot) return;
            const el = document.getElementById(slot);
            el.classList.add("empty");
            el.classList.remove("speaking");
            el.querySelector(".username").innerText = "Empty";
            peerSlotMap.delete(userId);
            participants.delete(userId);
            updateParticipantsUI();
        }
        function addChatMessage(fromUser, text, isMe = false) {
            const box = document.getElementById("chat-messages");
            const div = document.createElement("div");
            div.className = "message" + (isMe ? " me" : "");
            const name = isMe ? "You" : fromUser;
            div.innerHTML = `<strong>${name}:</strong> ${text}`;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }

        /***** Local media *****/
        async function initLocalMedia() {
            try {
                localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                document.getElementById("status").innerText = "Mic ready";
                document.getElementById("muteBtn").disabled = false;
                document.getElementById("unmuteBtn").disabled = false;
                assignSlot(USER, `You (${USER})`);
            } catch (e) {
                document.getElementById("status").innerText = "Mic access denied";
                console.warn("getUserMedia failed", e);
            }
        }

        /***** Peer connection helpers *****/
        function shouldInitiateWith(peerId) {
            // deterministic tie-breaker: lexicographic
            return String(USER) < String(peerId);
        }

        function resetPending(peerId) {
            remoteDescReady.set(peerId, false);
            pendingCandidates.set(peerId, pendingCandidates.get(peerId) || []);
        }

        function addCandidateToQueue(peerId, cand) {
            const q = pendingCandidates.get(peerId) || [];
            q.push(cand);
            pendingCandidates.set(peerId, q);
        }

        function flushCandidates(peerId, pc) {
            const q = pendingCandidates.get(peerId) || [];
            pendingCandidates.set(peerId, []);
            (async () => {
                for (const c of q) {
                    try { await pc.addIceCandidate(c); } catch (e) { console.warn("flush ICE err", e); }
                }
            })();
        }

        function closePeer(peerId) {
            const pc = peers.get(peerId);
            if (pc) {
                try { pc.close(); } catch (e) { console.warn(e); }
                peers.delete(peerId);
            }
            const a = document.getElementById("audio-" + peerId);
            if (a) a.remove();
            removeSlot(peerId);
            pendingCandidates.delete(peerId);
            remoteDescReady.delete(peerId);
            console.log("Closed peer", peerId);
        }

        async function createPeerConnection(peerId) {
            if (peers.has(peerId)) return peers.get(peerId);

            const pc = new RTCPeerConnection({
                iceServers: [{ urls: "stun:stun.l.google.com:19302" }]
            });

            resetPending(peerId);

            // add local tracks if available
            if (localStream) localStream.getTracks().forEach(track => pc.addTrack(track, localStream));

            pc.ontrack = (event) => {
                let audio = document.getElementById("audio-" + peerId);
                if (!audio) {
                    audio = document.createElement("audio");
                    audio.id = "audio-" + peerId;
                    audio.autoplay = true;
                    audio.playsInline = true;
                    document.body.appendChild(audio);
                }
                audio.srcObject = event.streams[0];
            };

            pc.onicecandidate = (event) => {
                if (event.candidate) {
                    // send candidate to the peer
                    ws?.send(JSON.stringify({
                        type: "ice-candidate",
                        to: peerId,
                        data: event.candidate
                    }));
                }
            };

            pc.onconnectionstatechange = () => {
                console.log("PC connectionState for", peerId, "=", pc.connectionState);
                if (pc.connectionState === "failed" || pc.connectionState === "disconnected" || pc.connectionState === "closed") {
                    closePeer(peerId);
                }
            };

            pc.onsignalingstatechange = () => {
                console.log("PC signalingState for", peerId, "=", pc.signalingState);
            };
            pc.oniceconnectionstatechange = () => {
                console.log("PC iceConnectionState for", peerId, "=", pc.iceConnectionState);
            };

            peers.set(peerId, pc);
            return pc;
        }

        /***** WebSocket / signaling *****/
        async function startWebSocket() {
            ws = new WebSocket(WS_URL);

            ws.onopen = () => {
                document.getElementById("status").innerText = "Connected";
                assignSlot(USER, `You (${USER})`);
                console.log("WebSocket connected");
            };

            ws.onmessage = async (ev) => {
                let msg;
                try {
                    msg = JSON.parse(ev.data);
                } catch (e) {
                    console.warn("Failed to parse WS message", e, ev.data);
                    return;
                }

                // Debug log
                console.log("WS RX:", msg.type, "from", msg.from || msg.user_id || "(server)");

                // chat
                if (msg.type === "chat") {
                    addChatMessage(msg.from, msg.text, msg.from === USER);
                    return;
                }

                // initial peers list
                if (msg.type === "peers") {
                    // small stagger to avoid signaling storms
                    await new Promise(r => setTimeout(r, 50));
                    for (const peer of msg.peers) {
                        const peerId = peer.user_id;
                        assignSlot(peerId, peer.name || peerId);
                        const pc = await createPeerConnection(peerId);

                        // Only one side should initiate to avoid glare
                        if (shouldInitiateWith(peerId)) {
                            try {
                                const offer = await pc.createOffer();
                                await pc.setLocalDescription(offer);
                                ws.send(JSON.stringify({ type: "offer", to: peerId, data: offer }));
                                console.log("Sent offer to", peerId);
                            } catch (e) {
                                console.warn("Failed to create/send offer to", peerId, e);
                            }
                        } else {
                            console.log("Waiting for offer from", peerId);
                        }
                    }
                    return;
                }

                // peer joined (single peer)
                if (msg.type === "peer-joined") {
                    const peerId = msg.user_id;
                    assignSlot(peerId, msg.name || peerId);
                    const pc = await createPeerConnection(peerId);
                    if (shouldInitiateWith(peerId)) {
                        try {
                            const offer = await pc.createOffer();
                            await pc.setLocalDescription(offer);
                            ws.send(JSON.stringify({ type: "offer", to: peerId, data: offer }));
                            console.log("Sent offer (peer-joined) to", peerId);
                        } catch (e) {
                            console.warn("Failed to create/send offer on peer-joined", e);
                        }
                    } else {
                        console.log("peer-joined: waiting for offer from", peerId);
                    }
                    return;
                }

                // incoming offer: create/replace pc if necessary, set remote, answer
                if (msg.type === "offer") {
                    const peerId = msg.from;
                    assignSlot(peerId, msg.name || peerId);

                    // If we already have a pc but it's not stable, restart cleanly
                    if (peers.has(peerId)) {
                        const existing = peers.get(peerId);
                        if (existing.signalingState !== "stable") {
                            console.warn("Existing PC not stable on incoming offer — restarting pc for", peerId, existing.signalingState);
                            closePeer(peerId);
                        }
                    }

                    const pc = await createPeerConnection(peerId);
                    try {
                        await pc.setRemoteDescription(new RTCSessionDescription(msg.data));
                    } catch (e) {
                        console.warn("setRemoteDescription(offer) failed for", peerId, e);
                        // give up on this offer; hope sender retries
                        return;
                    }

                    // mark remote desc ready and flush ICE queue
                    remoteDescReady.set(peerId, true);
                    flushCandidates(peerId, pc);

                    try {
                        const answer = await pc.createAnswer();
                        await pc.setLocalDescription(answer);
                        ws.send(JSON.stringify({ type: "answer", to: peerId, data: answer }));
                        console.log("Sent answer to", peerId);
                    } catch (e) {
                        console.warn("Failed to create/send answer to", peerId, e);
                    }
                    return;
                }

                // incoming answer: only accept when we're in have-local-offer
                if (msg.type === "answer") {
                    const peerId = msg.from;
                    console.log("RX answer from", peerId);
                    const pc = peers.get(peerId);
                    if (!pc) {
                        console.warn("Received answer for unknown PC from", peerId);
                        return;
                    }

                    // only accept answer if we are expecting it
                    const state = pc.signalingState;
                    console.log("pc.signalingState before answer:", state);
                    if (state === "have-local-offer" || state === "have-local-pranswer") {
                        try {
                            // avoid applying duplicate answer if already has remote
                            if (!pc.currentRemoteDescription || Object.keys(pc.currentRemoteDescription).length === 0) {
                                await pc.setRemoteDescription(new RTCSessionDescription(msg.data));
                                remoteDescReady.set(peerId, true);
                                flushCandidates(peerId, pc);
                                console.log("Applied remote answer for", peerId);
                            } else {
                                console.log("Skipping answer application: remoteDescription already present for", peerId);
                            }
                        } catch (e) {
                            console.warn("setRemoteDescription(answer) failed for", peerId, e);
                        }
                    } else {
                        console.warn("Ignoring answer from", peerId, "because signalingState is", state);
                    }
                    return;
                }

                // ICE candidate
                if (msg.type === "ice-candidate") {
                    const peerId = msg.from;
                    const pc = peers.get(peerId);
                    const candidate = new RTCIceCandidate(msg.data);
                    if (pc && remoteDescReady.get(peerId)) {
                        try {
                            await pc.addIceCandidate(candidate);
                        } catch (e) {
                            console.warn("addIceCandidate error", e);
                        }
                    } else {
                        // queue until remote desc is applied
                        addCandidateToQueue(peerId, candidate);
                    }
                    return;
                }

                if (msg.type === "peer-left") {
                    closePeer(msg.user_id);
                    return;
                }

                // unknown message type
                console.warn("Unknown WS message type:", msg.type);
            };

            ws.onclose = () => {
                document.getElementById("status").innerText = "Disconnected";
                console.log("WebSocket closed");
            };
            ws.onerror = (e) => {
                console.warn("WebSocket error", e);
            };
        }

        /***** Controls *****/
        document.getElementById("muteBtn").onclick = () => {
            localStream?.getAudioTracks().forEach(t => t.enabled = false);
            document.getElementById("status").innerText = "Muted";
        };
        document.getElementById("unmuteBtn").onclick = () => {
            localStream?.getAudioTracks().forEach(t => t.enabled = true);
            document.getElementById("status").innerText = "Unmuted";
        };
        document.getElementById("leaveBtn").onclick = async () => {
            try {
                await fetch(BACKEND_HTTP + "/leave_room", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ user_id: USER, room_code: ROOM })
                });
            } catch (e) {
                console.warn("leave_room failed", e);
            } finally {
                ws?.close();
                localStream?.getTracks().forEach(t => t.stop());
                if (FRONTEND_URL) { window.location.href = FRONTEND_URL; } else { window.location.href = "/"; }
            }
        };
        document.getElementById("sendBtn").onclick = () => {
            const input = document.getElementById("chatInput");
            const text = input.value.trim();
            if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
            ws.send(JSON.stringify({ type: "chat", text: text }));
            addChatMessage(USER, text, true);
            input.value = "";
        };

        /***** Safety & logging *****/
        window.addEventListener("unhandledrejection", (ev) => {
            console.warn("UnhandledPromiseRejection:", ev.reason);
        });

        /***** Start up *****/
        (async () => {
            await initLocalMedia();
            await startWebSocket();
        })();
    </script>
</body>
</html>

"""

@app.get("/room", response_class=HTMLResponse)
async def room_page(room_code: str, user_id: str):
    # validate to avoid joining random rooms/users
    room = rooms.get(room_code)
    if not room or user_id not in users or user_id not in room["members"]:
        return HTMLResponse("<h2>Invalid room or user. Please (re)join from the app.</h2>", status_code=400)
    page = ROOM_HTML.replace("{{FRONTEND_URL_JSON}}", json_dumps(FRONTEND_URL))
    return HTMLResponse(page)

# tiny helper without importing stdlib json in many places above
def json_dumps(x: str) -> str:
    # minimal JSON string escape for safe inline JS
    import json as _json
    return _json.dumps(x)
