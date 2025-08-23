// Load values injected in index.html
const { WS_URL, BACKEND_HTTP, ROOM, USER } = window.APP_CONFIG;

let localStream = null;
let ws = null;
const peers = new Map();  // userId -> RTCPeerConnection
const MAX_USERS = 4;
const userSlots = ["user1", "user2", "user3", "user4"];
const slotMap = {}; // userId -> slotIndex

// Initialize slots as empty
userSlots.forEach(id => {
    const slot = document.getElementById(id);
    slot.classList.add("empty");
    slot.querySelector(".username").innerText = "Empty";
});

async function initLocalMedia() {
    try {
        localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        document.getElementById('status').innerText = "Mic ready";
    } catch (e) {
        console.error("getUserMedia failed", e);
        document.getElementById('status').innerText = "Mic access denied";
    }
}

function assignUserSlot(userId) {
    for (let i = 0; i < MAX_USERS; i++) {
        const slot = document.getElementById(userSlots[i]);
        if (slot.classList.contains("empty")) {
            slot.classList.remove("empty");
            slot.querySelector(".username").innerText = userId;
            slotMap[userId] = i;
            return;
        }
    }
}

function removeUserSlot(userId) {
    const index = slotMap[userId];
    if (index !== undefined) {
        const slot = document.getElementById(userSlots[index]);
        slot.classList.add("empty");
        slot.classList.remove("speaking");
        slot.querySelector(".username").innerText = "Empty";
        delete slotMap[userId];
    }
}

function userSpeaking(userId) {
    const index = slotMap[userId];
    if (index !== undefined) {
        const slot = document.getElementById(userSlots[index]);
        slot.classList.add("speaking");
        setTimeout(() => slot.classList.remove("speaking"), 300);
    }
}

// Existing WebRTC functions...
// createPeerConnection, makeOffer, handleOffer, handleAnswer, handleCandidate, closePeer

async function startWebSocket() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => document.getElementById('status').innerText = "Connected to signaling";

    ws.onmessage = async (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'peers') {
            msg.peers.forEach(peerId => assignUserSlot(peerId));
            for (const peerId of msg.peers) await makeOffer(peerId);
        } else if (msg.type === 'peer-joined') {
            assignUserSlot(msg.user_id);
            await makeOffer(msg.user_id);
        } else if (msg.type === 'peer-left') {
            removeUserSlot(msg.user_id);
            closePeer(msg.user_id);
        } else if (msg.type === 'speaking') {
            userSpeaking(msg.user_id);
        } else if (msg.type === 'offer') {
            await handleOffer(msg.from, msg.data);
        } else if (msg.type === 'answer') {
            await handleAnswer(msg.from, msg.data);
        } else if (msg.type === 'ice-candidate') {
            await handleCandidate(msg.from, msg.data);
        }
    };

    ws.onclose = () => {
        document.getElementById('status').innerText = "Signaling disconnected";
        Object.keys(slotMap).forEach(userId => removeUserSlot(userId));
        for (const p of Array.from(peers.keys())) closePeer(p);
    };
}

// Controls
document.getElementById('muteBtn').onclick = () => {
    if (!localStream) return;
    localStream.getAudioTracks().forEach(t => t.enabled = false);
    document.getElementById('status').innerText = "Muted";
};
document.getElementById('unmuteBtn').onclick = () => {
    if (!localStream) return;
    localStream.getAudioTracks().forEach(t => t.enabled = true);
    document.getElementById('status').innerText = "Unmuted";
};
document.getElementById('leaveBtn').onclick = () => {
    if (ws) ws.close();
    if (localStream) localStream.getTracks().forEach(t => t.stop());
    document.getElementById('status').innerText = "Left call";
};

// Auto-start
(async () => {
    await initLocalMedia();
    await startWebSocket();
})();
