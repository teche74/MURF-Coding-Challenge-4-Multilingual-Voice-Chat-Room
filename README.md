# 🎙️ Multilingual Voice Chat Room  

*A project built for [Murf Coding Challenge 4](https://lu.ma/k97ic9gi?tk=EwdsYA)*  

<img width="1904" height="866" alt="Screenshot 2025-08-23 002616" src="https://github.com/user-attachments/assets/6ec9207b-a4cc-40a6-8af5-52112f12672e" />


## 🚀 Project Overview  
This project is a **real-time group voice chat application** where multiple users can join a virtual room and communicate with each other seamlessly — regardless of the language they speak.  

The goal is to break language barriers by combining **voice chat** with **translation technologies**, making conversations fluid and accessible to everyone.  

The app mimics the experience of a **group call (like WhatsApp/Zoom)** with essential features:  
- Automatic joining into a voice call on entry.  
- Controls for **Mute / Unmute / Leave**.  
- Support for **multiple users in a room**.  
- Real-time updates when users join or leave.  

---

## 📌 Features  
✅ Real-time **group voice calling**  
✅ **Multilingual support** (conceptual integration with Murf/translation APIs)  
✅ **Room-based architecture** (create/join rooms)  
✅ **WebRTC-powered audio streaming**  
✅ Simple and clean **frontend interface**  
✅ Backend with **WebSocket signaling**  

---

## 🏗️ Tech Stack  
- **Frontend:** HTML, CSS, JavaScript (WebRTC for audio streaming)  
- **Backend:** Python (FastAPI, aiohttp, WebSocket)  
- **Real-time Communication:** WebRTC, aioice  
- **Dev Setup:** Docker / Devcontainers (optional)  

---

## 📂 Repository Structure  
```
MURF-Coding-Challenge-4-Multilingual-Voice-Chat-Room/
│── backend/         # Server-side code (room management, signaling, audio handling)
│── frontend/        # Web interface (UI for joining rooms, mute/unmute/leave)
│── .devcontainer/   # Dev environment configs
│── requirements.txt # Python dependencies
│── README.md        # Project documentation (this file)
│── .gitignore
```

---

## ⚙️ Installation & Setup  

### 1️⃣ Clone the Repository  
```bash
git clone https://github.com/teche74/MURF-Coding-Challenge-4-Multilingual-Voice-Chat-Room.git
cd MURF-Coding-Challenge-4-Multilingual-Voice-Chat-Room
```

### 2️⃣ Setup Backend  
```bash
cd backend
python -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

### 3️⃣ Run Frontend  
Open `frontend/index.html` in your browser (or serve via a simple HTTP server).  

### 4️⃣ Join the Chat Room  
- Open the frontend in multiple browser tabs or devices.  
- Each user will auto-join the call.  
- Use **Mute/Unmute** or **Leave** as needed.  

---

## 🎯 Future Improvements  
- ✅ Fix mute/unmute/leave state syncing across clients.  
- ✅ Improve audio routing (no echo, stable transmission).  
- 🔲 Integrate **real-time translation** (speech-to-text + translation + text-to-speech).  
- 🔲 Add **UI improvements** (participant list, speaking indicators).  
- 🔲 Deploy to cloud (Heroku/Vercel + Render/EC2).  

---

## 🙌 Acknowledgements  
This project was developed as part of **[Murf Coding Challenge 4](https://lu.ma/k97ic9gi?tk=EwdsYA)**.  
Special thanks to the organizers for encouraging innovation in **multilingual, voice-first experiences**.  
