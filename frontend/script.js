// Load values injected in index.html
const { WS_URL, BACKEND_HTTP, ROOM, USER } = window.APP_CONFIG;

let localStream = null;
let ws = null;
const peers = new Map(); // userId -> RTCPeerConnection
const maxUsers = 4;
const userSlots = ["user1", "user2", "user3", "user4"];

// Map peerId to slot
const peerSlotMap = new Map();

async function initLocalMedia() {
    try {
        localStream = await navigator.mediaDevices.getUserMedia({
            audio: true,
            video: false,
        });
        document.getElementById("status").innerText = "Mic ready";
    } catch (e) {
        console.error("getUserMedia failed", e);
        document.getElementById("status").innerText = "Mic access denied";
    }
}

function assignSlot(peerId, name) {
    for (let slot of userSlots) {
        const el = document.getElementById(slot);
        if (el.classList.contains("empty")) {
            el.classList.remove("empty");
            el.querySelector(".username").innerText = name;
            peerSlotMap.set(peerId, slot);
            break;
        }
    }
}

function removeSlot(peerId) {
    const slot = peerSlotMap.get(peerId);
    if (!slot) return;
    const el = document.getElementById(slot);
    el.classList.add("empty");
    el.classList.remove("speaking");
    el.querySelector(".username").innerText = "Empty";
    peerSlotMap.delete(peerId);
}

function markSpeaking(peerId, speaking) {
    const slot = peerSlotMap.get(peerId);
    if (!slot) return;
    const el = document.getElementById(slot);
    if (speaking) el.classList.add("speaking");
    else el.classList.remove("speaking");
}

function createPeerConnection(peerId, peerName) {
    const pc = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });

    assignSlot(peerId, peerName);

    // Add local audio
    if (localStream && localStream.getAudioTracks().length > 0) {
        pc.addTrack(localStream.getAudioTracks()[0], localStream);
    }

    // Play remote audio
    pc.addEventListener("track", (ev) => {
        const stream = ev.streams[0];
        let audioEl = document.getElementById("audio-" + peerId);
        if (!audioEl) {
            audioEl = document.createElement("audio");
            audioEl.id = "audio-" + peerId;
            audioEl.autoplay = true;
            audioEl.controls = false;
            audioEl.style.display = "none";
            document.body.appendChild(audioEl);

            // Visual speaking detection
            const audioCtx = new AudioContext();
            const analyser = audioCtx.createAnalyser();
            const source = audioCtx.createMediaStreamSource(stream);
            source.connect(analyser);
            const data = new Uint8Array(analyser.frequencyBinCount);

            function checkSpeaking() {
                analyser.getByteFrequencyData(data);
                const volume = data.reduce((a, b) => a + b) / data.length;
                markSpeaking(peerId, volume > 10); // Threshold for speaking
                requestAnimationFrame(checkSpeaking);
            }
            checkSpeaking();
        }
        audioEl.srcObject = stream;
    });

    pc.onicecandidate = (ev) => {
        if (ev.candidate) {
            ws.send(
                JSON.stringify({
                    type: "ice-candidate",
                    to: peerId,
                    data: ev.candidate,
                })
            );
        }
    };

    pc.onconnectionstatechange = () => {
        if (["failed", "closed", "disconnected"].includes(pc.connectionState)) {
            closePeer(peerId);
        }
    };

    return pc;
}

async function makeOffer(peerId, peerName) {
    const pc = createPeerConnection(peerId, peerName);
    peers.set(peerId, pc);
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    ws.send(JSON.stringify({ type: "offer", to: peerId, data: offer }));
}

async function handleOffer(fromId, offer, name) {
    const pc = createPeerConnection(fromId, name);
    peers.set(fromId, pc);
    await pc.setRemoteDescription(new RTCSessionDescription(offer));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    ws.send(JSON.stringify({ type: "answer", to: fromId, data: answer }));
}

async function handleAnswer(fromId, answer) {
    const pc = peers.get(fromId);
    if (pc) await pc.setRemoteDescription(new RTCSessionDescription(answer));
}

async function handleCandidate(fromId, cand) {
    const pc = peers.get(fromId);
    if (pc) {
        try {
            await pc.addIceCandidate(new RTCIceCandidate(cand));
        } catch (e) {
            console.warn("Failed to add ICE candidate", e);
        }
    }
}

function closePeer(peerId) {
    const pc = peers.get(peerId);
    if (pc) {
        pc.getSenders().forEach((s) => {
            try {
                pc.removeTrack(s);
            } catch (e) {}
        });
        try {
            pc.close();
        } catch (e) {}
        peers.delete(peerId);
    }
    const a = document.getElementById("audio-" + peerId);
    if (a) a.remove();
    removeSlot(peerId);
}

async function startWebSocket() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        document.getElementById("status").innerText = "Connected to signaling";
    };

    ws.onmessage = async (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "peers") {
            for (const peer of msg.peers) {
                await makeOffer(peer.user_id, peer.name);
            }
        } else if (msg.type === "peer-joined") {
            await makeOffer(msg.user_id, msg.name);
        } else if (msg.type === "offer") {
            await handleOffer(msg.from, msg.data, msg.name);
        } else if (msg.type === "answer") {
            await handleAnswer(msg.from, msg.data);
        } else if (msg.type === "ice-candidate") {
            await handleCandidate(msg.from, msg.data);
        } else if (msg.type === "peer-left") {
            closePeer(msg.user_id);
        }
    };

    ws.onclose = () => {
        document.getElementById("status").innerText = "Signaling disconnected";
        for (const p of Array.from(peers.keys())) closePeer(p);
    };
}

// Controls
document.getElementById("muteBtn").onclick = () => {
    if (!localStream) return;
    localStream.getAudioTracks().forEach((t) => (t.enabled = false));
    document.getElementById("status").innerText = "Muted";
};

document.getElementById("unmuteBtn").onclick = () => {
    if (!localStream) return;
    localStream.getAudioTracks().forEach((t) => (t.enabled = true));
    document.getElementById("status").innerText = "Unmuted";
};

document.getElementById("leaveBtn").onclick = () => {
    if (ws) ws.close();
    if (localStream) localStream.getTracks().forEach((t) => t.stop());
    document.getElementById("status").innerText = "Left call";
};

// Auto-start
(async () => {
    await initLocalMedia();
    await startWebSocket();
})();
