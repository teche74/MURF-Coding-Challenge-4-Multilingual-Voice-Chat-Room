import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import streamlit as st
from streamlit_webrtc import webrtc_streamer, WebRtcMode, AudioProcessorBase
import requests
import av
import numpy as np
import threading
from collections import defaultdict, deque
import random

_ROOM_BUFFERS = defaultdict(list)  
_ROOM_LOCK = threading.Lock()
MAX_CHUNKS = 8  


class RoomAudioMixer(AudioProcessorBase):
    def __init__(self) -> None:
        self.room_code = None
        self.my_buf = deque(maxlen=MAX_CHUNKS)

    def on_start(self):
        self.room_code = st.session_state.get("room_code")
        if not self.room_code:
            return
        with _ROOM_LOCK:
            _ROOM_BUFFERS[self.room_code].append(self.my_buf)

    def on_ended(self):
        if not self.room_code:
            return
        with _ROOM_LOCK:
            try:
                _ROOM_BUFFERS[self.room_code].remove(self.my_buf)
                if not _ROOM_BUFFERS[self.room_code]:
                    del _ROOM_BUFFERS[self.room_code]
            except ValueError:
                pass

    def recv_audio(self, frame: av.AudioFrame) -> av.AudioFrame:
        pcm = frame.to_ndarray() 
        if pcm.ndim == 2:
            pcm = pcm.mean(axis=0, keepdims=True) 

        pcm = pcm.astype(np.float32)
        self.my_buf.append(pcm.copy())

        with _ROOM_LOCK:
            buffers = _ROOM_BUFFERS.get(self.room_code, [])
            other_streams = [b[-1] for b in buffers if (b is not self.my_buf and len(b) > 0)]

        if other_streams:
            min_len = min(s.shape[-1] for s in other_streams)
            stack = np.stack([s[..., :min_len] for s in other_streams], axis=0)
            mixed = stack.mean(axis=0)
        else:
            mixed = np.zeros_like(pcm)

        out = (mixed * 32767.0).clip(-32768, 32767).astype(np.int16)
        out_frame = av.AudioFrame.from_ndarray(out, layout="mono")
        out_frame.sample_rate = getattr(frame, "sample_rate", 48000)
        return out_frame


class AudioCallApp:
    def __init__(self):
        self.backend_url = "https://murf-coding-challenge-4-multilingual.onrender.com"
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
        st.subheader("Audio Call Room 🎤")
        st.write(f"Room Code: {st.session_state['room_code']}")

        webrtc_ctx = webrtc_streamer(
            key="audio_call",
            mode=WebRtcMode.SENDRECV,
            audio_receiver_size=1024,
            sendback_audio=True, 
            media_stream_constraints={"audio": True, "video": False},
            rtc_configuration={"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]},
            video_html_attrs={"style": {"display": "none"}},
            desired_playing_state=True,
            audio_processor_factory=RoomAudioMixer, 
        )

        col1, col2, col3 = st.columns([1, 1, 1])
        with col1:
            if st.button("🔇 Mute"):
                if webrtc_ctx and webrtc_ctx.state.playing:
                    webrtc_ctx.audio_receiver_enabled = False
                    self.muted = True
        with col2:
            if st.button("🎙️ Unmute"):
                if webrtc_ctx and webrtc_ctx.state.playing:
                    webrtc_ctx.audio_receiver_enabled = True
                    self.muted = False
        with col3:
            if st.button("❌ Leave Call"):
                if "room_code" in st.session_state:
                    del st.session_state["room_code"]
                st.rerun()

        try:
            resp = requests.get(f"{self.backend_url}/room_info?room_code={st.session_state['room_code']}")
            members = resp.json().get("members", [])
        except Exception:
            members = []

        active_speaker = random.choice(members) if members else None

        st.markdown("### Participants")
        num_members = max(1, len(members))
        cols_per_row = 2 if num_members <= 4 else 4
        rows = (num_members + cols_per_row - 1) // cols_per_row

        for r in range(rows):
            cols = st.columns(cols_per_row)
            for c in range(cols_per_row):
                idx = r * cols_per_row + c
                if idx < num_members:
                    user = members[idx]
                    card_class = "participant-card"
                    if user == active_speaker:
                        card_class += " active-speaker"
                    with cols[c]:
                        st.markdown(
                            f"""
                            <div class="{card_class}">
                                <div style='font-size:40px;'>👤</div>
                                <b>{user}</b><br>
                                {"🔇 Muted" if self.muted else "🎙️ Speaking..."}
                            </div>
                            """,
                            unsafe_allow_html=True
                        )
                else:
                    with cols[c]:
                        st.markdown(
                            "<div class='empty-slot'>Empty Slot</div>",
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
