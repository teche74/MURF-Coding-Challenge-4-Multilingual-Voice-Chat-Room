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
        /* Animated vibrant gradient background */
        .stApp {
            background: linear-gradient(-45deg, #6a11cb, #2575fc, #00c9ff, #92fe9d);
            background-size: 400% 400%;
            animation: gradientBG 12s ease infinite;
            color: white;
        }
        @keyframes gradientBG {
            0% {background-position: 0% 50%;}
            50% {background-position: 100% 50%;}
            100% {background-position: 0% 50%;}
        }

        /* Glassmorphism panel */
        .glass-card {
            padding: 30px;
            border-radius: 18px;
            background: rgba(255, 255, 255, 0.08);
            backdrop-filter: blur(12px);
            box-shadow: 0 8px 30px rgba(0,0,0,0.2);
            border: 1px solid rgba(255,255,255,0.15);
            margin: 20px 0;
        }

        /* Title with neon glow */
        .title {
            font-size: 2.8rem;
            font-weight: 800;
            text-align: center;
            margin-bottom: 25px;
            color: #ffffff;
            text-shadow: 0 0 15px rgba(255,255,255,0.8),
                         0 0 30px rgba(0, 180, 255, 0.7);
        }

        .subheader {
            font-size: 1.3rem;
            font-weight: 500;
            color: #f0f0f0;
            margin-bottom: 15px;
            text-align: center;
        }

        /* Modern buttons */
        .stButton>button {
            background: linear-gradient(135deg, #00f2fe, #4facfe);
            color: white;
            font-weight: 600;
            border-radius: 12px;
            padding: 0.7em 1.5em;
            border: none;
            transition: all 0.3s ease;
            box-shadow: 0 6px 18px rgba(0, 180, 255, 0.5);
        }
        .stButton>button:hover {
            transform: translateY(-3px) scale(1.05);
            box-shadow: 0 8px 25px rgba(0, 180, 255, 0.8);
        }

        /* Input box styling */
        .stTextInput>div>div>input {
            background: rgba(255,255,255,0.1);
            border-radius: 10px;
            border: 1px solid rgba(255,255,255,0.2);
            color: white;
        }

        /* Success messages glowing */
        .success {
            color: #4efc4e;
            font-weight: bold;
            text-shadow: 0 0 10px rgba(78, 252, 78, 0.7);
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

    def show_room_options(self):
        st.markdown("<div class='subheader'>🛠 Choose Your Room Option</div>", unsafe_allow_html=True)

        # Card layout container
        col1, col2 = st.columns(2)

        with col1:
            st.markdown('<div class="glass-card">', unsafe_allow_html=True)
            st.markdown("<h3>➕ Create Room</h3>", unsafe_allow_html=True)
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
            st.markdown('</div>', unsafe_allow_html=True)

        with col2:
            st.markdown('<div class="glass-card">', unsafe_allow_html=True)
            st.markdown("<h3>🔑 Join Room</h3>", unsafe_allow_html=True)
            room_code = st.text_input("Room Code")
            if st.button("➡️ Join Room"):
                resp = requests.post(f"{self.backend_url}/join_room",
                                    json={"user_id": st.session_state['user_id'], "room_code": room_code or None})
                if resp.status_code == 200:
                    st.session_state['room_code'] = resp.json()["room_code"]
                    st.success(f"✅ Joined room: `{st.session_state['room_code']}`")
                    st.rerun()
                else:
                    st.error(f"❌ Failed to join room: {resp.text}")
            st.markdown('</div>', unsafe_allow_html=True)

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
