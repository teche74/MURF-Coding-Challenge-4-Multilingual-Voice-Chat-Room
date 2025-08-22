import streamlit as st
import sounddevice as sd
import numpy as np
import websocket
import threading
import requests
import json
from utils import play_audio

class ChatApp:
    def __init__(self):
        self.backend_url = "http://localhost:8000"
        self.ws = None
        self.is_recording = False
        self.recording_thread = None
        self.stream = None
        self.receive_thread = None

    def login(self):
        st.title("Login With Google")
        if "user_id" in st.session_state and "name" in st.session_state:
            st.success(f"Logged in as {st.session_state['name']}")
            return

        language_options = {
            "English":"en","Spanish":"es","French":"fr","German":"de",
            "Italian":"it","Hindi":"hi","Portuguese":"pt","Dutch":"nl","Korean":"ko",
            "Chinese (Mandarin)":"zh","Bengali":"bn","Tamil":"ta","Polish":"pl",
            "Japanese":"ja","Turkish":"tr","Indonesian":"id","Croatian":"hr",
            "Greek":"el","Romanian":"ro","Slovak":"sk","Bulgarian":"bg"
        }

        selected_language_name = st.selectbox("Preferred Language", list(language_options.keys()))
        language = language_options[selected_language_name]
        login_url = f"{self.backend_url}/login/google?language={language}"
        if st.button("Login with Google"):
            st.markdown(f"[Click here to login with Google]({login_url})", unsafe_allow_html=True)
            st.info("Open the link above to login (a new tab will be used). After login you'll be redirected back.")

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
        room_code = st.text_input("Enter Room Code (optional)")
        if st.button("Join"):
            resp = requests.post(f"{self.backend_url}/join_room",
                                 json={"user_id": st.session_state['user_id'], "room_code": room_code or None})
            if resp.status_code == 200:
                st.session_state['room_code'] = resp.json()["room_code"]
                st.success(f"Joined room: {st.session_state['room_code']}")
                st.rerun()
            else:
                st.error(f"Failed to join room: {resp.text}")

    def setup_websocket(self):
        if self.ws:
            try:
                if getattr(self.ws, "connected", True):
                    return True
            except Exception:
                pass

        if "room_code" not in st.session_state or "user_id" not in st.session_state:
            st.error("Missing room_code or user_id in session. Please login and join/create a room first.")
            return False

        room_code = st.session_state['room_code']
        user_id = st.session_state['user_id']
        url = f"ws://localhost:8000/ws?room_code={room_code}&user_id={user_id}"

        try:
            self.ws = websocket.create_connection(url, timeout=5)
            st.success("Connected to voice chat!")

            def receive_audio():
                last_header = None
                while self.ws:
                    try:
                        data = self.ws.recv()
                        if isinstance(data, bytes):
                            if last_header and last_header.get("type") == "tts_audio":
                                try:
                                    play_audio(data, format_hint="mp3")
                                except Exception as e:
                                    print("Error playing tts mp3:", e)
                                last_header = None
                            else:
                                try:
                                    play_audio(data)
                                except Exception as e:
                                    print("Error playing forwarded audio:", e)
                        else:
                            try:
                                payload = json.loads(data)
                                if payload.get("type") == "tts_audio":
                                    last_header = payload
                                else:
                                    print("WS text message:", payload)
                            except Exception:
                                print("WS got text:", data)
                    except Exception as e:
                        print(f"WebSocket receive error (will close): {e}")
                        break

                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass
                self.ws = None
                print("Receive thread exiting, ws closed")

            self.receive_thread = threading.Thread(target=receive_audio, daemon=True)
            self.receive_thread.start()
            return True

        except Exception as e:
            st.error(f"Failed to connect to websocket: {e!r}")
            print("WebSocket connect exception:", repr(e))
            self.ws = None
            return False

    def start_recording(self):
        if not self.ws:
            st.warning("WebSocket not connected!")
            return

        self.is_recording = True
        chunk_size = 1024
        sample_rate = 16000

        def callback(indata, frames, time_info, status):
            if status:
                print("Input status:", status)
            
            if self.is_recording and self.ws and self.ws.connected:
                try:
                    audio_bytes = (indata * 32767).astype(np.int16).tobytes()
                    self.ws.send(audio_bytes)
                except Exception as e:
                    print(f"Error sending audio: {e}")

        def record():
            with sd.InputStream(samplerate=sample_rate,
                                channels=1,
                                blocksize=chunk_size,
                                callback=callback,
                                dtype=np.float32):
                while self.is_recording:
                    sd.sleep(100)

        self.recording_thread = threading.Thread(target=record, daemon=True)
        self.recording_thread.start()

    def stop_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False

    def run(self):
        if "user_id" not in st.session_state:
            self.login()
            return

        if "room_code" in st.session_state:
            st.subheader(f"Room: {st.session_state['room_code']}")
            if "ws_connected" not in st.session_state:
                st.session_state["ws_connected"] = self.setup_websocket()

            if not self.is_recording:
                if st.button("Start Talking"):
                    self.start_recording()
            else:
                if st.button("Stop Talking"):
                    self.stop_recording()

            try:
                resp = requests.get(f"{self.backend_url}/room_info?room_code={st.session_state['room_code']}")
                if resp.status_code == 200:
                    members = resp.json().get("members", [])
                    st.write(f"Users in room: {', '.join(members)}")
            except Exception:
                pass
        else:
            self.show_room_options()

def main():
    app = ChatApp()
    app.run()

if __name__ == "__main__":
    main()
