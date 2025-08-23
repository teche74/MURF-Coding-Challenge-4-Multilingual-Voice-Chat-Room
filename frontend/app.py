import sys, os, json
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import streamlit as st
import requests
from streamlit.components.v1 import html as st_html

BACKEND_URL = "https://murf-coding-challenge-4-multilingual.onrender.com" 


def ws_url_from_backend(burl: str):
    if burl.startswith("https://"):
        return "wss://" + burl[len("https://"):]
    if burl.startswith("http://"):
        return "ws://" + burl[len("http://"):]
    return burl

class AudioCallApp:
    def __init__(self):
        self.backend_url = BACKEND_URL
        self.muted = False

    def login(self):
        st.title("Login With Google")
        if "user_id" in st.session_state and "name" in st.session_state:
            st.success(f"Logged in as {st.session_state['name']}")
            return

        login_url = f"{self.backend_url}/login/google"
        if st.button("Login with Google"):
            st.markdown(f"[Click here to login with Google]({login_url})", unsafe_allow_html=True)

        query_params = st.query_params
        if "user_id" in query_params and "name" in query_params:
            st.session_state['user_id'] = query_params["user_id"]
            st.session_state['name'] = query_params["name"]
            st.success(f"Logged in as {st.session_state['name']}")
            st.rerun()

    def show_room_options(self):
        st.subheader("Room Options")
        option = st.radio("Choose:", ["Create Room", "Join Room"])
        if option == "Create Room":
            self.create_room()
        else:
            self.join_room()

    def create_room(self):
        public = st.checkbox("Public Room?", value=True)
        if st.button("Create"):
            resp = requests.post(f"{self.backend_url}/create_room",
                                 json={"user_id": st.session_state['user_id'], "public": public})
            if resp.status_code == 200:
                st.session_state['room_code'] = resp.json()["room_code"]
                st.success(f"Room created: {st.session_state['room_code']}")
                st.rerun()
            else:
                st.error(f"Failed to create room: {resp.text}")

    def join_room(self):
        room_code = st.text_input("Enter Room Code")
        if st.button("Join"):
            resp = requests.post(f"{self.backend_url}/join_room",
                                 json={"user_id": st.session_state['user_id'], "room_code": room_code or None})
            if resp.status_code == 200:
                st.session_state['room_code'] = resp.json()["room_code"]
                st.success(f"Joined room: {st.session_state['room_code']}")
                st.rerun()
            else:
                st.error(f"Failed to join room: {resp.text}")

    def run_audio_call(self):
        st.markdown(
            """
            <style>
            .participant-card {
                border: 2px solid #333;
                border-radius: 16px;
                padding: 20px;
                margin: 10px;
                background-color: #1e1e1e;
                color: white;
                text-align: center;
                box-shadow: 0px 0px 8px rgba(0,0,0,0.4);
                min-height: 120px;
                transition: all 0.2s ease-in-out;
            }
            .active-speaker {
                border: 2px solid #4CAF50 !important;
                box-shadow: 0px 0px 20px #4CAF50;
            }
            .empty-slot {
                border: 2px dashed #444;
                border-radius: 16px;
                padding: 20px;
                margin: 10px;
                color: #777;
                text-align: center;
                min-height: 120px;
            }
            audio { display: none; } /* hidden audio elements play remote audio */
            .control-bar {
                position: sticky; bottom: 0; padding: 12px; background: #0f0f0f; text-align:center;
            }
            .control-btn { margin: 0 8px; padding: 10px 16px; border-radius: 999px; border:none; cursor:pointer; }
            .leave { background: #b00020; color:#fff; }
            </style>
            """,
            unsafe_allow_html=True
        )

        st.subheader("Audio Call Room 🎤")
        room = st.session_state['room_code']
        user = st.session_state['user_id']

        try:
            info = requests.get(f"{self.backend_url}/room_info", params={"room_code": room}).json()
            members = info.get("members", [])
        except Exception:
            st.warning(f"Failed to fetch members: {e}")
            members = []
        st.caption(f"Room Code: {room} • Members: {len(members)}")

        ws_base = ws_url_from_backend(self.backend_url)
        ws_url = f"{ws_base}/ws?room_code={room}&user_id={user}"
        stun = "stun:stun.l.google.com:19302"

        st_html(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
</head>
<body>
<div id="controls" class="control-bar">
  <button id="muteBtn" class="control-btn">🔇 Mute</button>
  <button id="unmuteBtn" class="control-btn">🎙️ Unmute</button>
  <button id="leaveBtn" class="control-btn leave">❌ Leave</button>
  <span id="status" style="margin-left:12px;color:#ddd;"></span>
</div>

<script>
const WS_URL = {json.dumps(ws_url)};
const ICE_SERVERS = [{{ urls: "stun:stun.l.google.com:19302" }}];

let localStream = null;
let ws = null;
const peers = new Map();  // userId -> RTCPeerConnection

async function initLocalMedia() {{
  try {{
    localStream = await navigator.mediaDevices.getUserMedia({{ audio: true, video: false }});
    document.getElementById('status').innerText = "Mic ready";
  }} catch(e) {{
    console.error("getUserMedia failed", e);
    document.getElementById('status').innerText = "Mic access denied";
  }}
}}

function createPeerConnection(peerId) {{
  const pc = new RTCPeerConnection({{ iceServers: ICE_SERVERS }});

  // Add local audio track to send to peer
  if (localStream && localStream.getAudioTracks().length > 0) {{
    pc.addTrack(localStream.getAudioTracks()[0], localStream);
  }}

  // When we get remote track, create hidden audio element and play it
  pc.addEventListener('track', (ev) => {{
    const stream = ev.streams[0];
    let audioEl = document.getElementById('audio-' + peerId);
    if (!audioEl) {{
      audioEl = document.createElement('audio');
      audioEl.id = 'audio-' + peerId;
      audioEl.autoplay = true;
      audioEl.controls = false;
      audioEl.style.display = 'none';
      document.body.appendChild(audioEl);
    }}
    audioEl.srcObject = stream;
  }});

  pc.onicecandidate = (ev) => {{
    if (ev.candidate) {{
      ws.send(JSON.stringify({{ type: 'ice-candidate', to: peerId, data: ev.candidate }}));
    }}
  }};

  pc.onconnectionstatechange = () => {{
    if (pc.connectionState === 'failed' || pc.connectionState === 'closed' || pc.connectionState === 'disconnected') {{
      // cleanup
      closePeer(peerId);
    }}
  }};

  return pc;
}}

async function makeOffer(peerId) {{
  const pc = createPeerConnection(peerId);
  peers.set(peerId, pc);
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  ws.send(JSON.stringify({{ type: 'offer', to: peerId, data: offer }}));
}}

async function handleOffer(fromId, offer) {{
  const pc = createPeerConnection(fromId);
  peers.set(fromId, pc);
  await pc.setRemoteDescription(new RTCSessionDescription(offer));
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  ws.send(JSON.stringify({{ type: 'answer', to: fromId, data: answer }}));
}}

async function handleAnswer(fromId, answer) {{
  const pc = peers.get(fromId);
  if (pc) {{
    await pc.setRemoteDescription(new RTCSessionDescription(answer));
  }}
}}

async function handleCandidate(fromId, cand) {{
  const pc = peers.get(fromId);
  if (pc) {{
    try {{
      await pc.addIceCandidate(new RTCIceCandidate(cand));
    }} catch (e) {{
      console.warn("Failed to add ICE candidate", e);
    }}
  }}
}}

function closePeer(peerId) {{
  const pc = peers.get(peerId);
  if (pc) {{
    pc.getSenders().forEach(s => {{
      try {{ pc.removeTrack(s); }} catch(e){{}}
    }});
    try {{ pc.close(); }} catch(e){{}}
    peers.delete(peerId);
  }}
  const a = document.getElementById('audio-' + peerId);
  if (a) a.remove();
}}

async function startWebSocket() {{
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {{
    document.getElementById('status').innerText = "Connected to signaling";
  }};

  ws.onmessage = async (ev) => {{
    const msg = JSON.parse(ev.data);
    if (msg.type === 'peers') {{
      for (const peerId of msg.peers) {{
        await makeOffer(peerId);
      }}
    }} else if (msg.type === 'peer-joined') {{
      // small delay ok
      await makeOffer(msg.user_id);
    }} else if (msg.type === 'offer') {{
      await handleOffer(msg.from, msg.data);
    }} else if (msg.type === 'answer') {{
      await handleAnswer(msg.from, msg.data);
    }} else if (msg.type === 'ice-candidate') {{
      await handleCandidate(msg.from, msg.data);
    }} else if (msg.type === 'peer-left') {{
      closePeer(msg.user_id);
    }}
  }};

  ws.onclose = () => {{
    document.getElementById('status').innerText = "Signaling disconnected";
    // cleanup peers
    for (const p of Array.from(peers.keys())) closePeer(p);
  }};
}}

document.getElementById('muteBtn').onclick = () => {{
  if (!localStream) return;
  localStream.getAudioTracks().forEach(t => t.enabled = false);
  document.getElementById('status').innerText = "Muted";
}};

document.getElementById('unmuteBtn').onclick = () => {{
  if (!localStream) return;
  localStream.getAudioTracks().forEach(t => t.enabled = true);
  document.getElementById('status').innerText = "Unmuted";
}};

document.getElementById('leaveBtn').onclick = () => {{
  if (ws) ws.close();
  if (localStream) localStream.getTracks().forEach(t => t.stop());
  document.getElementById('status').innerText = "Left call";
}};

// Auto-start
(async () => {{
  await initLocalMedia();
  await startWebSocket();
}})();
</script>
</body>
</html>
        """, height=120, scrolling=False)

        st.markdown("### Participants")
        cols_per_row = 2 if len(members) <= 4 else 4
        rows = (len(members) + cols_per_row - 1) // cols_per_row or 1
        for r in range(rows):
            cols = st.columns(cols_per_row)
            for c in range(cols_per_row):
                i = r*cols_per_row + c
                if i < len(members):
                    with cols[c]:
                        st.markdown(
                            f"<div class='participant-card'>👤 <b>{members[i]}</b><br/><span style='opacity:.7'>Connected</span></div>",
                            unsafe_allow_html=True
                        )
                else:
                    with cols[c]:
                        st.markdown(
                            "<div class='empty-slot'>Empty</div>",
                            unsafe_allow_html=True
                        )

    def run(self):
        if "user_id" not in st.session_state:
            self.login()
            return
        if "room_code" not in st.session_state:
            self.show_room_options()
        else:
            self.run_audio_call()

def main():
    app = AudioCallApp()
    app.run()

if __name__ == "__main__":
    main()
