"""Equalizer band table and the libmpv audio-filter string.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

BANDS = (60, 170, 310, 600, 1000, 3000, 6000, 12000, 14000, 16000)
BAND_LABELS = ("60", "170", "310", "600", "1K", "3K", "6K", "12K", "14K", "16K")

# Graphic-EQ presets in dB, one value per band. Preamp stays separate.
PRESETS = {
    "Flat": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "Classical": (0, 0, 0, 0, 0, 0, -4, -4, -4, -6),
    "Club": (0, 0, 2, 4, 4, 4, 2, 0, 0, 0),
    "Dance": (6, 4, 1, 0, 0, -3, -4, -4, 0, 0),
    "Full Bass": (6, 6, 6, 3, 0, -2, -4, -6, -6, -6),
    "Full Treble": (-6, -6, -6, -2, 1, 5, 8, 8, 8, 8),
    "Laptop": (3, 6, 3, -2, -2, 0, 2, 4, 6, 7),
    "Live": (-3, 0, 2, 3, 3, 3, 2, 1, 1, 1),
    "Party": (5, 5, 0, 0, 0, 0, 0, 0, 5, 5),
    "Pop": (-1, 2, 4, 4, 2, -1, -1, -1, -1, -1),
    "Reggae": (0, 0, 0, -2, 0, 3, 4, 0, 0, 0),
    "Rock": (5, 3, -2, -3, -1, 2, 4, 5, 5, 5),
    "Soft": (2, 1, 0, -1, -1, 1, 2, 3, 4, 4),
    "Techno": (5, 4, 0, -3, -2, 0, 4, 6, 6, 5),
}

EQ_MIN = -12.0
EQ_MAX = 12.0


def clamp_db(value: float) -> float:
    """Keep a gain inside the equalizer range, in half-decibel steps."""
    value = float(value)
    if value < EQ_MIN:
        return EQ_MIN
    if value > EQ_MAX:
        return EQ_MAX
    # Half-dB steps keep the filter graph stable while a slider is moving.
    return round(value * 2.0) / 2.0


def build_af(eq_on: bool, preamp: float, gains, balance: float) -> str:
    """Return an mpv ``af`` value, or "" when the signal should stay untouched."""
    parts = []
    if eq_on:
        preamp = clamp_db(preamp)
        if abs(preamp) >= 0.25:
            parts.append(f"volume={preamp:.2f}dB")
        for freq, gain in zip(BANDS, gains):
            gain = clamp_db(gain)
            if abs(gain) >= 0.25:
                parts.append(f"equalizer=f={int(freq)}:t=o:w=1.0:g={gain:.2f}")
    balance = max(-1.0, min(1.0, float(balance)))
    if abs(balance) >= 0.01:
        left = 1.0 - max(0.0, balance)
        right = 1.0 + min(0.0, balance)
        parts.append(f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1")
    if not parts:
        return ""
    return "lavfi=[" + ",".join(parts) + "]"
