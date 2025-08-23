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
        st.subheader("Audio Call Room 🎤")
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
