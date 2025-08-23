import streamlit as st
from streamlit_webrtc import webrtc_streamer, WebRtcMode
import requests

class AudioCallApp:
    def __init__(self):
        self.backend_url = "https://chatfree.streamlit.app/"
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
                min-height: 150px;
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
                min-height: 150px;
            }
            audio, video {
                display: none !important;
            }
            button[title="Stop"], button[title="Start"] {
                display: none !important;
            }
            .stAudio, .stVideo, button[title="Start"], button[title="Stop"] {
                display: none !important;
            }
            </style>
            """,
            unsafe_allow_html=True
        )

        st.subheader("Audio Call Room 🎤")
        st.write(f"Room Code: {st.session_state['room_code']}")

        # WebRTC audio-only
        webrtc_ctx = webrtc_streamer(
            key="audio_call",
            mode=WebRtcMode.SENDRECV,
            audio_receiver_size=1024,
            sendback_audio=False,
            media_stream_constraints={"audio": True, "video": False},
            rtc_configuration={"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]},
            video_html_attrs={"style": {"display": "none"}},
            desired_playing_state=True
        )

        # Controls
        col1, col2, col3 = st.columns([1,1,1])

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

        # Get members
        try:
            resp = requests.get(f"{self.backend_url}/room_info?room_code={st.session_state['room_code']}")
            members = resp.json().get("members", [])
        except Exception:
            members = []

        # Simulated active speaker (you can replace with real audio-level detection later)
        import random
        active_speaker = random.choice(members) if members else None

        # WhatsApp-like grid
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
