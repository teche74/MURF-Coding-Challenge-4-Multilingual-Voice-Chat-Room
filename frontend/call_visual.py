import numpy as np
import streamlit as st
import streamlit.components.v1 as components

def fft_bars_from_pcm16(audio_bytes: bytes, sample_rate: int = 16000, bands: int = 6):
    """
    Convert raw 16-bit PCM mono audio bytes to `bands` normalized levels (0..1).
    Tuned for ~16 kHz mic/voice stream. Stateless and fast.
    """
    if not audio_bytes:
        return [0.0] * bands

    x = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
    if x.size == 0:
        return [0.0] * bands

    # Window to reduce spectral leakage
    w = np.hanning(x.size)
    X = np.fft.rfft(x * w)
    mag = np.abs(X)

    # Frequency bins (rfft)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)

    # Voice-ish range and log-like band edges
    lo, hi = 80, min(8000, sample_rate // 2)
    edges = np.geomspace(max(1, lo), max(lo + 1, hi), bands + 1)

    out = []
    eps = 1e-9
    total_max = float(np.max(mag) + eps)
    for i in range(bands):
        f0, f1 = edges[i], edges[i + 1]
        idx = np.where((freqs >= f0) & (freqs < f1))[0]
        band_val = float(np.sqrt(np.mean((mag[idx] if idx.size else [0.0]) ** 2)))
        out.append(band_val / total_max)

    # Smooth-ish curve, cap to 1
    out = [min(1.0, v ** 0.8) for v in out]
    return out

def render_eq_html(levels, label=""):
    """
    Render a compact 'voice pulse' tile: avatar dot + multi-band vertical bars.
    Pure HTML/CSS; Streamlit-safe; no external deps.
    """
    lv = [max(0.0, min(1.0, float(x))) for x in (levels or [])]
    heights = [int(12 + 88 * v) for v in lv]  # keep a visible floor
    bars_html = "".join(f'<div class="bar" style="height:{h}%"></div>' for h in heights) or \
                '<div class="bar" style="height:12%"></div>' * 6

    html = f"""
    <div class="tile">
      <div class="avatar-ring"><div class="dot"></div></div>
      <div class="label">{label}</div>
      <div class="eq">{bars_html}</div>
    </div>
    <style>
      .tile {{
        display:flex; flex-direction:column; align-items:center;
        gap:10px; padding:14px; border-radius:16px;
        background:#0b0f12; border:1px solid #1f2a33;
        box-shadow: 0 0 0 1px rgba(0,0,0,0.35) inset;
        width: 180px;
      }}
      .label {{ color:#e6f1f5; font-size:0.95rem; font-weight:600; }}
      .eq {{
        display:flex; align-items:flex-end; gap:6px; height:64px; width:100%;
        padding: 0 6px;
      }}
      .bar {{
        flex:1; min-width:8px; border-radius:6px 6px 2px 2px;
        background: linear-gradient(180deg, #4be1a0, #25a0d9);
        transition: height 120ms ease;
        filter: drop-shadow(0 2px 4px rgba(0,0,0,0.25));
      }}
      .avatar-ring {{
        height:42px; width:42px; border-radius:999px; position:relative;
        background: radial-gradient(60% 60% at 50% 50%, #0f1720 0%, #0a0f13 100%);
        border:1px solid #20303a; box-shadow:0 0 0 3px rgba(37,160,217,0.12);
      }}
      .dot {{
        position:absolute; inset: 8px; border-radius:999px; background:#1f2a33;
        box-shadow: inset 0 0 12px rgba(37,160,217,0.18);
      }}
    </style>
    """
    components.html(html, height=150, scrolling=False)
