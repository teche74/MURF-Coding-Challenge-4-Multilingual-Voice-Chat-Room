import sys, os, json
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import streamlit as st
import requests
from streamlit.components.v1 import html as st_html

BACKEND_URL = "https://murf-coding-challenge-4-multilingual.onrender.com" 

def local_css():
    st.markdown(
        """
        <style>
        /* Dark gradient background */
        .stApp {
            background: linear-gradient(to right, #0f0f0f, #1c1c1c, #2b2b2b);
        }

        /* Title: White and bold */
        .title {
            font-size: 2.2rem;
            font-weight: bold;
            color: #ffffff;
            text-align: center;
            margin-bottom: 20px;
        }

        /* Subheader: softer white/grey */
        .subheader {
            font-size: 1.4rem;
            color: #e0e0e0;
            margin-top: 10px;
        }

        /* Buttons: white text on dark blue */
        .stButton>button {
            background: #1a73e8;
            color: white;
            font-weight: 600;
            border-radius: 10px;
            padding: 0.6em 1.2em;
            transition: all 0.3s ease;
        }
        .stButton>button:hover {
            background: #0f5bd1;
            transform: scale(1.05);
        }

        /* Room container: dark card style */
        .room-box {
            padding: 20px;
            border-radius: 15px;
            background: #121212;
            box-shadow: 0 4px 10px rgba(0,0,0,0.4);
            margin: 20px 0;
            color: #f5f5f5;
        }

        /* Success text in light green */
        .success {
            color: #4caf50;
            font-weight: bold;
        }
        </style>
        """,
        unsafe_allow_html=True
    )

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
        local_css()

    def login(self):
        st.markdown(
        """
        <style>
        /* Make background full screen animated gradient */
        .stApp {
            background: linear-gradient(270deg, #0f2027, #203a43, #2c5364);
            background-size: 600% 600%;
            animation: gradientShift 15s ease infinite;
        }
        @keyframes gradientShift {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }

        /* Darken Streamlit header and sidebar */
        header[data-testid="stHeader"] {
            background: linear-gradient(to right, #141E30, #243B55);
        }
        section[data-testid="stSidebar"] {
            background: #1c1c1c;
        }
        </style>
            """,
            unsafe_allow_html=True,
        )

        st.title("✨ 🎧 Welcome to Multilingual Audio ChatRoom ✨")
        st.subheader("Login With Google to Continue")

        if "user_id" in st.session_state and "name" in st.session_state:
            st.success(f"✅ Logged in as {st.session_state['name']}")
            return

        login_url = f"{self.backend_url}/login/google"

        if st.button("🔑 Login with Google", use_container_width=True):
            st.markdown(f"[Click here to login with Google]({login_url})", unsafe_allow_html=True)

        query_params = st.query_params
        if "user_id" in query_params and "name" in query_params:
            st.session_state['user_id'] = query_params["user_id"]
            st.session_state['name'] = query_params["name"]
            st.success(f"✅ Logged in as {st.session_state['name']}")
            st.rerun()

    def show_room_options(self):
        st.markdown("<div class='subheader'>🛠 Room Options</div>", unsafe_allow_html=True)

        option = st.radio("Select an option", ["➕ Create Room", "🔑 Join Room"])
        st.markdown("<div class='room-box'>", unsafe_allow_html=True)
        if option == "➕ Create Room":
            self.create_room()
        else:
            self.join_room()
        st.markdown("</div>", unsafe_allow_html=True)

    def create_room(self):
        public = st.checkbox("🌍 Public Room?", value=True)
        if st.button("🚀 Create Room"):
            resp = requests.post(f"{self.backend_url}/create_room",
                                 json={"user_id": st.session_state['user_id'], "public": public})
            if resp.status_code == 200:
                st.session_state['room_code'] = resp.json()["room_code"]
                st.success(f"🎉 Room created: `{st.session_state['room_code']}`")
                st.rerun()
            else:
                st.error(f"❌ Failed to create room: {resp.text}")

    def join_room(self):
        room_code = st.text_input("Enter Room Code 🔢")
        if st.button("➡️ Join Room"):
            resp = requests.post(f"{self.backend_url}/join_room",
                                 json={"user_id": st.session_state['user_id'], "room_code": room_code or None})
            if resp.status_code == 200:
                st.session_state['room_code'] = resp.json()["room_code"]
                st.success(f"✅ Joined room: `{st.session_state['room_code']}`")
                st.rerun()
            else:
                st.error(f"❌ Failed to join room: {resp.text}")

    def run_audio_call(self):
        st.markdown("<div class='subheader'>🎤 Audio Call Room</div>", unsafe_allow_html=True)
        
        room = st.session_state['room_code']
        user = st.session_state['user_id']

        ws_base = ws_url_from_backend(self.backend_url)
        ws_url = f"{ws_base}/ws?room_code={room}&user_id={user}"

        with open(os.path.join("frontend", "index.html")) as f:
            html_code = f.read()

        html_code = html_code.replace("{{WS_URL}}", json.dumps(ws_url))
        html_code = html_code.replace("{{BACKEND_HTTP}}", json.dumps(self.backend_url))
        html_code = html_code.replace("{{ROOM}}", json.dumps(room))
        html_code = html_code.replace("{{USER}}", json.dumps(user))

        st_html(html_code, height=800, scrolling=True)

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
