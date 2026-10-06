#!/usr/bin/env python3
"""Synthesize the viewer's wooden move click as a short WAV (50 ms, 48 kHz mono, 16-bit).

Same recipe as playClick() in llm_tournament_viewer.py: a band-passed noise burst (1.8 kHz, Q 2.5,
fast decay) plus a short sine body falling 260 -> 120 Hz. Peak level is set in dBFS so clicks stay
clearly under the speech.

Usage: click_sound.py <out.wav> [--peak-db -20]
"""
import argparse
import wave

import numpy as np

SR = 48000


def make_click(peak_db=-20.0, sr=SR, seconds=0.05, seed=7):
    n = int(sr * seconds)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / sr
    noise = rng.uniform(-1, 1, n) * (1 - np.arange(n) / n) ** 5
    f0, q = 1800.0, 2.5
    w0 = 2 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2 * q)
    b0, b2 = alpha / (1 + alpha), -alpha / (1 + alpha)
    a1, a2 = -2 * np.cos(w0) / (1 + alpha), (1 - alpha) / (1 + alpha)
    y = np.zeros(n)
    for i in range(n):
        y[i] = b0 * noise[i] + (b2 * noise[i - 2] if i >= 2 else 0) \
            - (a1 * y[i - 1] if i >= 1 else 0) - (a2 * y[i - 2] if i >= 2 else 0)
    freq = 260 * (120 / 260) ** (t / 0.07)
    body = np.sin(2 * np.pi * np.cumsum(freq) / sr) * np.exp(-t * 60) * 0.5
    click = y * 0.7 * np.exp(-t * 90) / max(1e-9, np.abs(y).max()) + body
    click = click / np.abs(click).max()
    return click * 10 ** (peak_db / 20)


def write_wav(path, samples, sr=SR):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--peak-db", type=float, default=-20.0)
    a = ap.parse_args()
    write_wav(a.out, make_click(a.peak_db))
    print(a.out)
