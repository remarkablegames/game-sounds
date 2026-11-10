#!/usr/bin/env python3
"""Generate a procedural game sound pack using only the Python standard library.

Outputs 44.1 kHz mono 16-bit WAV masters, plus optional OGG/MP3 copies via
ffmpeg. Sounds are rendered from deterministic seeds so every run is
reproducible.
"""

import argparse
import json
import math
import random
import shutil
import subprocess
import sys
import wave
from array import array
from collections.abc import Iterable, Sequence
from pathlib import Path

SAMPLE_RATE = 44_100
BASE_SEED = 1337
STYLES = ("retro", "modern", "soft", "realistic")
FORMATS = ("wav", "ogg", "mp3")
TAU = math.tau

# (kind, variant) pairs rendered for every style.
SOUNDS: tuple[tuple[str, str], ...] = (
    ("jump", "01"),
    ("jump", "02"),
    ("walk", "01"),
    ("walk", "02"),
    ("walk", "03"),
    ("walk", "04"),
    ("hit", "light"),
    ("hit", "medium"),
    ("hit", "heavy"),
    ("land", "01"),
    ("land", "02"),
    ("swing", "01"),
    ("swing", "02"),
    ("hurt", "01"),
    ("hurt", "02"),
    ("death", "01"),
    ("pickup", "01"),
    ("pickup", "02"),
    ("ui_click", "01"),
    ("ui_hover", "01"),
    ("ui_confirm", "01"),
    ("ui_back", "01"),
    ("ui_error", "01"),
    ("ui_open", "01"),
    ("ui_close", "01"),
    ("ui_notification", "01"),
    ("victory", "01"),
    ("defeat", "01"),
    ("level_up", "01"),
    ("achievement", "01"),
    ("shoot", "01"),
    ("shoot", "02"),
    ("explosion", "01"),
    ("powerup", "01"),
    ("shield", "01"),
    ("dash", "01"),
    ("heal", "01"),
    ("enemy_alert", "01"),
    ("splash", "01"),
)

# ---------------------------------------------------------------------------
# DSP primitives
# ---------------------------------------------------------------------------


def samples_for(duration: float) -> int:
    return max(1, round(duration * SAMPLE_RATE))


def silence(duration: float) -> list[float]:
    return [0.0] * samples_for(duration)


def scale(layer: Sequence[float], gain: float) -> list[float]:
    return [value * gain for value in layer]


def mix(*layers: Sequence[float]) -> list[float]:
    """Sum layers, zero-padding shorter ones."""
    if not layers:
        return []
    longest = max(len(layer) for layer in layers)
    out = [0.0] * longest
    for layer in layers:
        for index, value in enumerate(layer):
            out[index] += value
    return out


def offset(layer: Sequence[float], seconds: float) -> list[float]:
    """Prepend silence so a layer starts later."""
    return silence(seconds) + list(layer)


def wave_sample(kind: str, phase: float, duty: float = 0.5) -> float:
    position = phase % 1.0
    if kind == "sine":
        return math.sin(TAU * position)
    if kind == "square":
        return 1.0 if position < duty else -1.0
    if kind == "triangle":
        return 4.0 * abs(position - 0.5) - 1.0
    if kind == "saw":
        return 2.0 * position - 1.0
    raise ValueError(f"unknown waveform: {kind}")


def perc_env(n: int, attack: float = 0.004, tau: float = 0.08) -> list[float]:
    """Linear attack followed by exponential decay."""
    attack_samples = max(1, min(n, int(attack * SAMPLE_RATE)))
    decay_rate = 1.0 / (max(tau, 1e-4) * SAMPLE_RATE)
    env = [0.0] * n
    for index in range(n):
        if index < attack_samples:
            env[index] = index / attack_samples
        else:
            env[index] = math.exp(-(index - attack_samples) * decay_rate)
    return env


def swell_env(n: int, peak: float = 0.5, power: float = 1.5) -> list[float]:
    """Rise to `peak` then fall symmetrically (for whooshes)."""
    peak = min(max(peak, 0.0), 1.0)
    env = [0.0] * n
    for index in range(n):
        t = index / (n - 1) if n > 1 else 0.0
        if t <= peak:
            env[index] = (t / peak) ** power if peak > 0.0 else 1.0
        else:
            env[index] = ((1.0 - t) / (1.0 - peak)) ** power if peak < 1.0 else 1.0
    return env


def tone(
    duration: float,
    f_start: float,
    f_end: float,
    kind: str = "sine",
    duty: float = 0.5,
    attack: float = 0.004,
    tau: float | None = None,
    vibrato: float = 0.0,
    vibrato_rate: float = 6.0,
    env: Sequence[float] | None = None,
) -> list[float]:
    """Oscillator with an exponential pitch glide and an optional envelope."""
    n = samples_for(duration)
    if env is None:
        env = perc_env(
            n, attack, tau if tau is not None else max(0.02, duration * 0.35)
        )

    f_start = max(1.0, f_start)
    f_end = max(1.0, f_end)
    ratio = f_end / f_start
    phase = 0.0
    out = [0.0] * n
    for index in range(n):
        t = index / (n - 1) if n > 1 else 0.0
        frequency = f_start * (ratio**t)
        if vibrato:
            frequency *= 1.0 + vibrato * math.sin(
                TAU * vibrato_rate * index / SAMPLE_RATE
            )
        phase += frequency / SAMPLE_RATE
        out[index] = wave_sample(kind, phase, duty) * env[index]
    return out


def noise(
    duration: float,
    rng: random.Random,
    attack: float = 0.002,
    tau: float = 0.05,
    env: Sequence[float] | None = None,
) -> list[float]:
    n = samples_for(duration)
    if env is None:
        env = perc_env(n, attack, tau)
    return [rng.uniform(-1.0, 1.0) * env[index] for index in range(n)]


def lowpass(layer: Sequence[float], cutoff: float) -> list[float]:
    if cutoff <= 0.0:
        return [0.0] * len(layer)
    if cutoff >= SAMPLE_RATE / 2:
        return list(layer)
    alpha = 1.0 - math.exp(-TAU * cutoff / SAMPLE_RATE)
    out = [0.0] * len(layer)
    state = 0.0
    for index, value in enumerate(layer):
        state += alpha * (value - state)
        out[index] = state
    return out


def highpass(layer: Sequence[float], cutoff: float) -> list[float]:
    if cutoff <= 0.0:
        return list(layer)
    if cutoff >= SAMPLE_RATE / 2:
        return [0.0] * len(layer)
    alpha = 1.0 - math.exp(-TAU * cutoff / SAMPLE_RATE)
    out = [0.0] * len(layer)
    state = 0.0
    previous = 0.0
    for index, value in enumerate(layer):
        state = alpha * (state + value - previous)
        previous = value
        out[index] = state
    return out


def bandpass(layer: Sequence[float], low: float, high: float) -> list[float]:
    """Broad band-pass built from two one-pole low-passes."""
    low_part = lowpass(layer, low)
    high_part = lowpass(layer, high)
    return [high_part[index] - low_part[index] for index in range(len(layer))]


def moving_lowpass(layer: Sequence[float], f_start: float, f_end: float) -> list[float]:
    """One-pole low-pass whose cutoff glides exponentially."""
    n = len(layer)
    out = [0.0] * n
    state = 0.0
    f_start = max(1.0, f_start)
    f_end = max(1.0, f_end)
    ratio = f_end / f_start
    for index, value in enumerate(layer):
        t = index / (n - 1) if n > 1 else 0.0
        cutoff = f_start * (ratio**t)
        alpha = 1.0 - math.exp(-TAU * cutoff / SAMPLE_RATE)
        state += alpha * (value - state)
        out[index] = state
    return out


def resonant_bandpass(
    layer: Sequence[float], frequency: float, q: float = 1.0
) -> list[float]:
    """RBJ biquad band-pass (constant skirt gain, peak gain = Q)."""
    w0 = TAU * frequency / SAMPLE_RATE
    alpha = math.sin(w0) / (2.0 * q)
    cosine = math.cos(w0)

    b0 = alpha
    b1 = 0.0
    b2 = -alpha
    a0 = 1.0 + alpha
    a1 = -2.0 * cosine
    a2 = 1.0 - alpha

    b0 /= a0
    b1 /= a0
    b2 /= a0
    a1 /= a0
    a2 /= a0

    out = [0.0] * len(layer)
    x1 = x2 = y1 = y2 = 0.0
    for index, value in enumerate(layer):
        result = b0 * value + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[index] = result
        x2, x1 = x1, value
        y2, y1 = y1, result
    return out


def softclip(layer: Sequence[float], drive: float = 1.0) -> list[float]:
    if drive <= 0.0:
        return list(layer)
    normalizer = math.tanh(drive)
    return [math.tanh(drive * value) / normalizer for value in layer]


def bitcrush(layer: Sequence[float], bits: int = 7, hold: int = 2) -> list[float]:
    """Quantize amplitude and hold samples to lower the effective sample rate."""
    step = 1.0 / (2 ** (bits - 1))
    hold = max(1, hold)
    out = [0.0] * len(layer)
    held = 0.0
    for index, value in enumerate(layer):
        if index % hold == 0:
            value = max(-1.0, min(1.0, value))
            held = round(value / step) * step
        out[index] = held
    return out


def bell(
    duration: float,
    frequency: float,
    ratios: Sequence[float] = (1.0, 2.01, 3.02),
    gains: Sequence[float] = (1.0, 0.4, 0.2),
    attack: float = 0.002,
    tau: float | None = None,
) -> list[float]:
    """Additive bell/ping with faster-decaying upper partials."""
    layers = []
    for ratio, gain in zip(ratios, gains):
        partial_tau = tau if tau is not None else duration * 0.35 / ratio
        partial = tone(
            duration,
            frequency * ratio,
            frequency * ratio,
            "sine",
            attack=attack,
            tau=partial_tau,
        )
        layers.append(scale(partial, gain))
    return mix(*layers)


def sequence(
    notes: Iterable[tuple[float, float]],
    kind: str = "sine",
    duty: float = 0.5,
    attack: float = 0.003,
    tau: float = 0.1,
) -> list[float]:
    out: list[float] = []
    for frequency, duration in notes:
        out.extend(
            tone(
                duration, frequency, frequency, kind, duty=duty, attack=attack, tau=tau
            )
        )
    return out


# ---------------------------------------------------------------------------
# Sound recipes
# ---------------------------------------------------------------------------


def make_jump(style: str, rng: random.Random, variant: str) -> list[float]:
    number = int(variant)

    if style == "retro":
        duration = 0.17 if number == 1 else 0.20
        base = 220 if number == 1 else 262
        top = 880 if number == 1 else 1047
        lead = tone(duration, base, top, "square", duty=0.5, attack=0.002, tau=0.075)
        harmony = tone(
            duration, base * 2, top * 2, "square", duty=0.125, attack=0.002, tau=0.05
        )
        air = highpass(noise(0.05, rng, 0.001, 0.012), 2500)
        return bitcrush(
            mix(lead, scale(harmony, 0.3), scale(air, 0.18)), bits=7, hold=2
        )

    if style == "modern":
        duration = 0.20
        lead = tone(duration, 170, 640, "saw", attack=0.006, tau=0.09)
        detuned = tone(duration, 176, 660, "saw", attack=0.008, tau=0.09)
        sub = tone(duration, 80, 145, "sine", attack=0.004, tau=0.12)
        air = highpass(noise(0.10, rng, 0.006, 0.035), 2200)
        body = mix(
            scale(lead, 0.5),
            scale(detuned, 0.3),
            scale(sub, 0.45),
            scale(air, 0.16),
        )
        return softclip(lowpass(body, 5500), 1.4)

    if style == "soft":
        duration = 0.30
        lead = tone(duration, 330, 700, "sine", attack=0.015, tau=0.16)
        harmonic = tone(duration, 660, 1400, "triangle", attack=0.015, tau=0.10)
        air = lowpass(noise(0.14, rng, 0.02, 0.06), 1600)
        return lowpass(mix(lead, scale(harmonic, 0.15), scale(air, 0.10)), 4200)

    # realistic: a rushing rise plus a small body thump
    duration = 0.24
    air = bandpass(noise(duration, rng, 0.03, 0.09), 300, 4000)
    air = moving_lowpass(air, 500, 2600)
    body = tone(0.13, 115, 62, "sine", attack=0.003, tau=0.045)
    return lowpass(mix(scale(air, 1.4), scale(body, 0.35)), 6000)


def make_walk(style: str, rng: random.Random, variant: str) -> list[float]:
    number = int(variant)

    if style == "retro":
        duration = 0.07 + 0.008 * number
        thump = tone(
            duration, 165 - 7 * number, 70, "square", duty=0.5, attack=0.001, tau=0.022
        )
        scuff = bitcrush(noise(duration * 0.85, rng, 0.001, 0.016), bits=6, hold=3)
        return mix(thump, scale(scuff, 0.55))

    if style == "modern":
        duration = 0.10
        thump = tone(duration, 120 + 5 * number, 55, "sine", attack=0.0015, tau=0.04)
        scuff = bandpass(noise(0.075, rng, 0.0015, 0.022), 500, 2800)
        tick = tone(
            0.018, 1900 + 90 * number, 1300, "triangle", attack=0.0005, tau=0.006
        )
        body = mix(scale(thump, 0.85), scale(scuff, 0.45), scale(tick, 0.12))
        return softclip(body, 1.1)

    if style == "soft":
        duration = 0.12
        thump = tone(duration, 100 + 5 * number, 55, "sine", attack=0.005, tau=0.05)
        scuff = lowpass(noise(0.085, rng, 0.004, 0.028), 1100)
        return lowpass(mix(thump, scale(scuff, 0.35)), 2400)

    duration = 0.11
    thump = tone(duration, 95 + 6 * number, 50, "sine", attack=0.0015, tau=0.045)
    scuff = bandpass(noise(0.09, rng, 0.003, 0.032), 550, 2600)
    body = resonant_bandpass(noise(0.06, rng, 0.001, 0.018), 280 + 30 * number, q=1.3)
    return lowpass(mix(scale(thump, 0.7), scale(scuff, 0.85), scale(body, 0.3)), 6500)


def make_hit(style: str, rng: random.Random, weight: str) -> list[float]:
    index = {"light": 0, "medium": 1, "heavy": 2}[weight]
    duration = (0.12, 0.20, 0.32)[index]
    f_start = (230.0, 205.0, 180.0)[index]
    f_end = (75.0, 58.0, 42.0)[index]

    if style == "retro":
        body = tone(
            duration,
            f_start,
            f_end,
            "square",
            duty=0.5,
            attack=0.001,
            tau=0.045 + 0.02 * index,
        )
        crunch = noise(duration * 0.6, rng, 0.0005, 0.018 + 0.006 * index)
        layers = [body, scale(crunch, 0.65 + 0.15 * index)]
        if index > 0:
            layers.append(
                scale(
                    tone(
                        duration, f_start * 2, f_end * 2, "saw", attack=0.001, tau=0.03
                    ),
                    0.22,
                )
            )
        return bitcrush(mix(*layers), bits=6, hold=2)

    if style == "modern":
        sub = tone(
            duration, f_start, f_end, "sine", attack=0.001, tau=0.06 + 0.035 * index
        )
        transient = highpass(noise(0.07, rng, 0.0005, 0.012 + 0.006 * index), 1400)
        body = tone(
            duration * 0.7,
            f_start * 1.4,
            f_end * 1.6,
            "saw",
            attack=0.0015,
            tau=0.04 + 0.025 * index,
        )
        layers = [sub, scale(transient, 0.55), scale(lowpass(body, 3200), 0.35)]
        return lowpass(softclip(mix(*layers), 1.6 + 0.3 * index), 7000)

    if style == "soft":
        thump = tone(
            duration,
            f_start * 0.7,
            f_end * 0.8,
            "sine",
            attack=0.004,
            tau=0.055 + 0.03 * index,
        )
        scuff = lowpass(noise(duration * 0.7, rng, 0.003, 0.03 + 0.02 * index), 1500)
        return lowpass(mix(thump, scale(scuff, 0.3)), 3000)

    transient = bandpass(noise(0.08, rng, 0.0005, 0.012 + 0.005 * index), 900, 5000)
    sub = tone(duration, f_start, f_end, "sine", attack=0.001, tau=0.055 + 0.03 * index)
    resonance = resonant_bandpass(
        noise(duration * 0.7, rng, 0.001, 0.04 + 0.03 * index), 170 + 20 * index, q=1.8
    )
    layers = [scale(transient, 0.8), scale(sub, 0.75), scale(resonance, 0.35)]
    if index == 2:
        debris = bandpass(noise(0.28, rng, 0.002, 0.12), 700, 3500)
        layers.append(scale(debris, 0.18))
    return lowpass(mix(*layers), 8000)


def make_land(style: str, rng: random.Random, variant: str) -> list[float]:
    number = int(variant)

    if style == "retro":
        duration = 0.13 + 0.015 * number
        body = tone(
            duration, 190 - 10 * number, 65, "square", duty=0.5, attack=0.001, tau=0.035
        )
        grit = bitcrush(noise(0.07, rng, 0.001, 0.02), bits=6, hold=3)
        return mix(body, scale(grit, 0.45))

    if style == "modern":
        duration = 0.18
        body = tone(duration, 130 - 5 * number, 50, "sine", attack=0.0015, tau=0.06)
        scuff = bandpass(noise(0.10, rng, 0.002, 0.035), 400, 2400)
        return softclip(lowpass(mix(scale(body, 0.8), scale(scuff, 0.45)), 5000), 1.2)

    if style == "soft":
        duration = 0.22
        body = tone(duration, 105 - 4 * number, 50, "sine", attack=0.006, tau=0.075)
        scuff = lowpass(noise(0.12, rng, 0.005, 0.04), 1000)
        return lowpass(mix(body, scale(scuff, 0.3)), 2200)

    duration = 0.20
    body = tone(duration, 105 - 4 * number, 45, "sine", attack=0.002, tau=0.06)
    scuff = bandpass(noise(0.12, rng, 0.003, 0.04), 500, 2800)
    return lowpass(mix(scale(body, 0.75), scale(scuff, 0.7)), 6000)


def make_swing(style: str, rng: random.Random, variant: str) -> list[float]:
    number = int(variant)
    duration = 0.24 + 0.05 * number
    n = samples_for(duration)
    env = swell_env(n, 0.5, 1.5)
    raw = [rng.uniform(-1.0, 1.0) * env[index] for index in range(n)]

    whoosh = bandpass(raw, 350 + 120 * number, 2200 + 600 * number)
    whoosh = moving_lowpass(whoosh, 500 + 100 * number, 2600 + 700 * number)

    if style == "retro":
        tonal = tone(
            duration,
            260 + 40 * number,
            700 + 100 * number,
            "square",
            duty=0.25,
            env=env,
        )
        return bitcrush(mix(scale(whoosh, 1.0), scale(tonal, 0.25)), bits=7, hold=2)

    if style == "modern":
        tonal = tone(duration, 220, 760, "saw", env=env)
        return softclip(lowpass(mix(scale(whoosh, 1.1), scale(tonal, 0.3)), 4000), 1.2)

    if style == "soft":
        tonal = tone(duration, 300, 620, "sine", env=env)
        return lowpass(mix(scale(whoosh, 0.9), scale(tonal, 0.2)), 3000)

    return lowpass(mix(scale(whoosh, 1.3), bandpass(raw, 200, 900)), 6000)


def make_hurt(style: str, rng: random.Random, variant: str) -> list[float]:
    number = int(variant)

    if style == "retro":
        duration = 0.16 + 0.02 * number
        lead = tone(
            duration,
            520 - 40 * number,
            180,
            "square",
            duty=0.25,
            attack=0.002,
            tau=0.06,
        )
        hit = noise(0.05, rng, 0.0005, 0.012)
        return bitcrush(mix(lead, scale(hit, 0.4)), bits=6, hold=2)

    if style == "modern":
        duration = 0.22
        lead = tone(duration, 420 - 30 * number, 130, "saw", attack=0.004, tau=0.09)
        sub = tone(duration, 160, 75, "sine", attack=0.003, tau=0.11)
        body = mix(
            scale(lead, 0.45),
            scale(sub, 0.4),
            scale(noise(0.06, rng, 0.001, 0.015), 0.25),
        )
        return softclip(lowpass(body, 4000), 1.5)

    if style == "soft":
        duration = 0.28
        lead = tone(duration, 500 - 35 * number, 220, "sine", attack=0.01, tau=0.12)
        return lowpass(mix(lead, scale(noise(0.08, rng, 0.005, 0.03), 0.18)), 3000)

    duration = 0.18
    lead = tone(duration, 380 - 30 * number, 140, "triangle", attack=0.003, tau=0.07)
    hit = bandpass(noise(0.06, rng, 0.001, 0.015), 400, 2200)
    return lowpass(mix(scale(lead, 0.4), scale(hit, 0.7)), 5000)


def make_death(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        notes = (
            (523.0, 0.08),
            (415.0, 0.08),
            (330.0, 0.09),
            (247.0, 0.10),
            (165.0, 0.14),
            (110.0, 0.22),
        )
        out = sequence(notes, "square", duty=0.5, attack=0.002, tau=0.1)
        out.extend(scale(noise(0.25, rng, 0.002, 0.08), 0.25))
        return bitcrush(out, bits=6, hold=2)

    if style == "modern":
        duration = 0.85
        lead = tone(
            duration,
            380,
            45,
            "saw",
            attack=0.005,
            tau=0.35,
            vibrato=0.01,
            vibrato_rate=5,
        )
        sub = tone(duration, 190, 32, "sine", attack=0.005, tau=0.4)
        body = mix(
            scale(lead, 0.4), scale(sub, 0.5), scale(noise(0.3, rng, 0.01, 0.18), 0.15)
        )
        return softclip(lowpass(body, 3500), 1.4)

    if style == "soft":
        duration = 0.9
        lead = tone(duration, 330, 90, "sine", attack=0.02, tau=0.4)
        harmonic = tone(duration, 165, 45, "triangle", attack=0.02, tau=0.35)
        return lowpass(mix(lead, scale(harmonic, 0.25)), 2200)

    duration = 0.7
    fall = tone(duration, 240, 55, "triangle", attack=0.004, tau=0.3)
    thud = tone(0.25, 95, 40, "sine", attack=0.002, tau=0.09)
    debris = bandpass(noise(0.45, rng, 0.005, 0.18), 300, 2000)
    return lowpass(mix(scale(fall, 0.4), scale(thud, 0.8), scale(debris, 0.3)), 5000)


def make_pickup(style: str, rng: random.Random, variant: str) -> list[float]:
    number = int(variant)

    if style == "retro":
        if number == 1:
            notes = ((988.0, 0.055), (1319.0, 0.14))
        else:
            notes = ((880.0, 0.05), (1109.0, 0.05), (1319.0, 0.14))
        return bitcrush(
            sequence(notes, "square", duty=0.25, attack=0.001, tau=0.09), bits=7, hold=2
        )

    if style == "modern":
        if number == 1:
            notes = ((1047.0, 0.09), (1568.0, 0.20))
        else:
            notes = ((880.0, 0.07), (1175.0, 0.07), (1760.0, 0.20))
        out: list[float] = []
        for frequency, duration in notes:
            out.extend(
                bell(
                    duration,
                    frequency,
                    ratios=(1.0, 2.0, 3.01),
                    gains=(1.0, 0.35, 0.15),
                )
            )
        return softclip(lowpass(out, 8000), 1.1)

    if style == "soft":
        if number == 1:
            notes = ((784.0, 0.11), (1175.0, 0.24))
        else:
            notes = ((698.0, 0.10), (1047.0, 0.12), (1397.0, 0.24))
        lead = sequence(notes, "sine", attack=0.006, tau=0.14)
        shimmer = sequence(notes, "triangle", attack=0.006, tau=0.1)
        return lowpass(mix(lead, scale(shimmer, 0.12)), 4000)

    if number == 1:
        base = 1250.0
        ratios = (1.0, 1.48, 2.31, 3.17)
        gains = (1.0, 0.5, 0.3, 0.18)
    else:
        base = 1560.0
        ratios = (1.0, 1.41, 2.19, 3.09)
        gains = (1.0, 0.45, 0.25, 0.15)
    ping = bell(0.35, base, ratios=ratios, gains=gains, attack=0.001, tau=0.12)
    strike = bandpass(noise(0.05, rng, 0.0005, 0.012), 2000, 9000)
    return lowpass(mix(ping, scale(strike, 0.5)), 12_000)


# (shape, start Hz, end Hz, duration seconds) for the short interface sounds.
UI_SOUNDS: dict[str, tuple[str, float, float, float]] = {
    "ui_click": ("tick", 1250.0, 900.0, 0.05),
    "ui_hover": ("tick", 1900.0, 1650.0, 0.03),
    "ui_confirm": ("rise", 740.0, 1109.0, 0.16),
    "ui_back": ("fall", 740.0, 494.0, 0.16),
    "ui_error": ("buzz", 196.0, 147.0, 0.22),
    "ui_open": ("up", 300.0, 900.0, 0.17),
    "ui_close": ("down", 900.0, 320.0, 0.17),
    "ui_notification": ("ping", 1047.0, 1047.0, 0.34),
}


def make_ui(style: str, rng: random.Random, kind: str) -> list[float]:
    shape, start, end, duration = UI_SOUNDS[kind]

    if style == "retro":
        if shape == "tick":
            body = tone(
                duration,
                start,
                end,
                "square",
                duty=0.5,
                attack=0.001,
                tau=duration * 0.4,
            )
            tick = highpass(noise(0.018, rng, 0.0005, 0.006), 3000)
            return bitcrush(mix(body, scale(tick, 0.2)), bits=7, hold=2)
        if shape in ("rise", "fall"):
            glide = tone(
                duration,
                start,
                end,
                "square",
                duty=0.25,
                attack=0.001,
                tau=duration * 0.45,
            )
            return bitcrush(glide, bits=7, hold=2)
        if shape == "buzz":
            low = tone(
                duration,
                start,
                end,
                "square",
                duty=0.5,
                attack=0.002,
                tau=duration * 0.5,
            )
            grit = tone(
                duration,
                start * 1.5,
                end * 1.4,
                "saw",
                attack=0.002,
                tau=duration * 0.3,
            )
            return bitcrush(mix(low, scale(grit, 0.35)), bits=5, hold=3)
        if shape == "ping":
            ping = bell(
                duration,
                start,
                ratios=(1.0, 2.0, 3.01),
                gains=(1.0, 0.35, 0.15),
                attack=0.001,
                tau=0.12,
            )
            return bitcrush(ping, bits=7, hold=2)
        env = swell_env(samples_for(duration), 0.45 if shape == "up" else 0.55)
        sweep = tone(duration, start, end, "square", duty=0.25, env=env)
        air = bandpass(
            noise(duration, rng, env=env), start, 5000 if shape == "up" else 3000
        )
        return bitcrush(mix(sweep, scale(air, 0.3)), bits=7, hold=2)

    if style == "modern":
        if shape == "tick":
            body = tone(duration, start, end, "sine", attack=0.001, tau=duration * 0.4)
            tick = highpass(noise(0.02, rng, 0.0005, 0.007), 2500)
            return lowpass(mix(body, scale(tick, 0.3)), 9000)
        if shape in ("rise", "fall"):
            body = tone(
                duration, start, end, "triangle", attack=0.002, tau=duration * 0.5
            )
            edge = tone(
                duration,
                start * 1.5,
                end * 1.4,
                "saw",
                attack=0.002,
                tau=duration * 0.3,
            )
            return lowpass(softclip(mix(body, scale(edge, 0.25)), 1.2), 6500)
        if shape == "buzz":
            low = tone(duration, start, end, "saw", attack=0.002, tau=duration * 0.5)
            detune = tone(
                duration,
                start * 1.03,
                end * 1.03,
                "saw",
                attack=0.003,
                tau=duration * 0.45,
            )
            return lowpass(softclip(mix(low, detune), 1.8), 3000)
        if shape == "ping":
            ping = bell(
                duration,
                start,
                ratios=(1.0, 2.01, 3.02),
                gains=(1.0, 0.3, 0.14),
                attack=0.001,
                tau=0.13,
            )
            strike = highpass(noise(0.03, rng, 0.0005, 0.008), 3000)
            return lowpass(mix(ping, scale(strike, 0.25)), 10_000)
        env = swell_env(samples_for(duration), 0.5 if shape == "up" else 0.45)
        sweep = tone(duration, start, end, "saw", attack=0.004, env=env)
        air = bandpass(noise(duration, rng, env=env), start, 6000)
        return lowpass(softclip(mix(scale(sweep, 0.7), scale(air, 0.4)), 1.2), 7000)

    if style == "soft":
        if shape == "tick":
            return lowpass(
                tone(duration, start, end, "sine", attack=0.002, tau=duration * 0.4),
                5000,
            )
        if shape in ("rise", "fall"):
            body = tone(duration, start, end, "sine", attack=0.004, tau=duration * 0.5)
            harmonic = tone(
                duration,
                start * 2,
                end * 2,
                "triangle",
                attack=0.004,
                tau=duration * 0.3,
            )
            return lowpass(mix(body, scale(harmonic, 0.12)), 4200)
        if shape == "buzz":
            body = tone(
                duration, start, end, "triangle", attack=0.005, tau=duration * 0.5
            )
            edge = tone(
                duration,
                start * 1.5,
                end * 1.5,
                "sine",
                attack=0.005,
                tau=duration * 0.35,
            )
            return lowpass(mix(body, scale(edge, 0.3)), 2400)
        if shape == "ping":
            return lowpass(
                bell(
                    duration,
                    start,
                    ratios=(1.0, 2.0, 3.01),
                    gains=(1.0, 0.2, 0.08),
                    attack=0.005,
                    tau=0.16,
                ),
                4500,
            )
        env = swell_env(samples_for(duration), 0.5)
        sweep = tone(duration, start, end, "sine", env=env)
        air = lowpass(noise(duration, rng, env=env), 2200)
        return lowpass(mix(sweep, scale(air, 0.15)), 3500)

    if shape == "tick":
        click = resonant_bandpass(
            noise(duration + 0.01, rng, 0.0005, duration * 0.8), start, q=4.0
        )
        tick = highpass(noise(0.015, rng, 0.0005, 0.005), 2000)
        return lowpass(mix(click, scale(tick, 0.4)), 12_000)
    if shape in ("rise", "fall"):
        body = tone(duration, start, end, "triangle", attack=0.002, tau=duration * 0.4)
        ring = resonant_bandpass(
            noise(duration, rng, 0.001, duration * 0.5), end, q=3.0
        )
        return lowpass(mix(scale(body, 0.5), ring), 9000)
    if shape == "buzz":
        body = tone(duration, start, end, "triangle", attack=0.003, tau=duration * 0.5)
        grit = resonant_bandpass(
            noise(duration, rng, 0.002, duration * 0.6), start, q=2.0
        )
        return lowpass(mix(scale(body, 0.5), grit), 3500)
    if shape == "ping":
        ping = bell(
            duration,
            start,
            ratios=(1.0, 1.48, 2.31, 3.17),
            gains=(1.0, 0.4, 0.2, 0.1),
            attack=0.001,
            tau=0.14,
        )
        strike = bandpass(noise(0.04, rng, 0.0005, 0.01), 2000, 9000)
        return lowpass(mix(ping, scale(strike, 0.4)), 12_000)

    env = swell_env(samples_for(duration), 0.45 if shape == "up" else 0.55)
    if shape == "up":
        air = moving_lowpass(noise(duration, rng, env=env), start * 1.4, end * 2.4)
    else:
        air = moving_lowpass(noise(duration, rng, env=env), start * 2.4, end * 1.4)
    knock = resonant_bandpass(
        noise(0.04, rng, 0.0005, 0.012), (start + end) * 0.5, q=3.0
    )
    return lowpass(mix(air, scale(knock, 0.4)), 10_000)


def make_victory(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        lead = sequence(
            ((523.0, 0.09), (659.0, 0.09), (784.0, 0.09), (1047.0, 0.28)),
            "square",
            duty=0.5,
            attack=0.002,
            tau=0.14,
        )
        harmony = sequence(
            ((392.0, 0.09), (494.0, 0.09), (587.0, 0.09), (784.0, 0.28)),
            "square",
            duty=0.25,
            attack=0.002,
            tau=0.12,
        )
        finale = offset(
            mix(
                tone(0.8, 1047.0, 1047.0, "square", attack=0.003, tau=0.7),
                scale(
                    tone(0.8, 784.0, 784.0, "square", duty=0.25, attack=0.003, tau=0.6),
                    0.35,
                ),
            ),
            0.66,
        )
        return bitcrush(
            mix(scale(lead, 0.7), scale(harmony, 0.3), scale(finale, 0.4)),
            bits=7,
            hold=2,
        )

    if style == "modern":
        duration = 1.3
        chord = mix(
            tone(duration, 523.0, 523.0, "saw", attack=0.01, tau=0.55),
            tone(duration, 659.0, 659.0, "saw", attack=0.01, tau=0.55),
            tone(duration, 784.0, 784.0, "saw", attack=0.01, tau=0.55),
            tone(duration, 528.0, 531.0, "saw", attack=0.012, tau=0.5),
        )
        arp = sequence(
            ((523.0, 0.10), (659.0, 0.10), (784.0, 0.10), (1047.0, 0.12)),
            "triangle",
            attack=0.003,
            tau=0.12,
        )
        riser = tone(0.4, 200.0, 523.0, "sine", attack=0.005, tau=0.2)
        body = mix(
            scale(arp, 0.5), scale(riser, 0.25), scale(offset(chord, 0.34), 0.35)
        )
        return lowpass(softclip(body, 1.3), 6500)

    if style == "soft":
        notes = ((523.0, 0.16), (659.0, 0.16), (784.0, 0.18), (1047.0, 0.65))
        lead = sequence(notes, "sine", attack=0.008, tau=0.28)
        shimmer = sequence(notes, "triangle", attack=0.008, tau=0.2)
        return lowpass(mix(lead, scale(shimmer, 0.14)), 3600)

    arp = mix(
        bell(0.3, 523.0, attack=0.001, tau=0.09),
        offset(bell(0.3, 659.0, attack=0.001, tau=0.09), 0.16),
        offset(bell(0.3, 784.0, attack=0.001, tau=0.09), 0.32),
        offset(
            bell(
                0.75,
                1047.0,
                ratios=(1.0, 2.0, 3.01, 4.2),
                gains=(1.0, 0.35, 0.18, 0.09),
                attack=0.001,
                tau=0.24,
            ),
            0.48,
        ),
    )
    body = tone(0.5, 130.0, 120.0, "sine", attack=0.003, tau=0.2)
    strike = noise(0.04, rng, 0.0005, 0.012)
    return lowpass(mix(scale(arp, 0.9), scale(body, 0.3), scale(strike, 0.3)), 12_000)


def make_defeat(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        lead = sequence(
            ((523.0, 0.12), (415.0, 0.12), (330.0, 0.15), (220.0, 0.6)),
            "square",
            duty=0.5,
            attack=0.002,
            tau=0.14,
        )
        harmony = sequence(
            ((392.0, 0.12), (311.0, 0.12), (247.0, 0.15), (165.0, 0.6)),
            "square",
            duty=0.25,
            attack=0.002,
            tau=0.12,
        )
        low = tone(1.05, 110.0, 55.0, "square", duty=0.5, attack=0.004, tau=0.7)
        return bitcrush(
            mix(scale(lead, 0.65), scale(harmony, 0.28), scale(low, 0.25)),
            bits=6,
            hold=2,
        )

    if style == "modern":
        first = tone(
            0.3,
            392.0,
            311.0,
            "saw",
            attack=0.006,
            tau=0.16,
            vibrato=0.008,
            vibrato_rate=4.5,
        )
        second = tone(
            0.5,
            294.0,
            196.0,
            "saw",
            attack=0.008,
            tau=0.24,
            vibrato=0.01,
            vibrato_rate=4.0,
        )
        sub = tone(1.0, 147.0, 49.0, "sine", attack=0.006, tau=0.5)
        debris = bandpass(noise(0.55, rng, 0.01, 0.22), 250, 1600)
        body = mix(
            scale(first, 0.5),
            scale(offset(second, 0.26), 0.55),
            scale(sub, 0.45),
            scale(debris, 0.12),
        )
        return lowpass(softclip(body, 1.35), 3400)

    if style == "soft":
        first = tone(0.32, 392.0, 330.0, "sine", attack=0.012, tau=0.2)
        second = tone(0.65, 294.0, 196.0, "sine", attack=0.02, tau=0.32)
        harmony = tone(0.65, 147.0, 98.0, "triangle", attack=0.02, tau=0.3)
        body = mix(first, offset(second, 0.2), scale(harmony, 0.25))
        return lowpass(body, 2400)

    fall = mix(
        bell(
            0.3,
            392.0,
            ratios=(1.0, 1.48, 2.31),
            gains=(1.0, 0.4, 0.2),
            attack=0.001,
            tau=0.09,
        ),
        offset(
            bell(
                0.3,
                311.0,
                ratios=(1.0, 1.48, 2.31),
                gains=(1.0, 0.4, 0.2),
                attack=0.001,
                tau=0.09,
            ),
            0.16,
        ),
        offset(
            bell(
                0.4,
                262.0,
                ratios=(1.0, 1.48, 2.31),
                gains=(1.0, 0.4, 0.2),
                attack=0.001,
                tau=0.11,
            ),
            0.32,
        ),
        offset(
            bell(
                0.7,
                196.0,
                ratios=(1.0, 1.48, 2.31, 3.17),
                gains=(1.0, 0.4, 0.2, 0.1),
                attack=0.001,
                tau=0.2,
            ),
            0.50,
        ),
    )
    sub = tone(0.9, 110.0, 42.0, "sine", attack=0.004, tau=0.4)
    debris = bandpass(noise(0.6, rng, 0.005, 0.22), 250, 1800)
    return lowpass(mix(scale(fall, 0.8), scale(sub, 0.5), scale(debris, 0.2)), 7000)


def make_level_up(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        notes = ((587.0, 0.07), (740.0, 0.07), (880.0, 0.07), (1175.0, 0.26))
        lead = sequence(notes, "square", duty=0.25, attack=0.001, tau=0.1)
        sparkle = offset(
            tone(0.22, 1760.0, 1760.0, "square", duty=0.125, attack=0.001, tau=0.12),
            0.4,
        )
        return bitcrush(mix(lead, scale(sparkle, 0.2)), bits=7, hold=2)

    if style == "modern":
        arp = mix(
            bell(0.18, 659.0, attack=0.001, tau=0.07),
            offset(bell(0.18, 784.0, attack=0.001, tau=0.07), 0.07),
            offset(bell(0.2, 988.0, attack=0.001, tau=0.09), 0.14),
            offset(bell(0.5, 1319.0, attack=0.001, tau=0.18), 0.21),
        )
        sub = tone(0.5, 90.0, 130.0, "sine", attack=0.01, tau=0.2)
        shimmer = highpass(noise(0.4, rng, 0.02, 0.15), 4000)
        return lowpass(
            softclip(mix(scale(arp, 0.9), scale(sub, 0.3), scale(shimmer, 0.06)), 1.1),
            10_000,
        )

    if style == "soft":
        notes = ((587.0, 0.1), (740.0, 0.1), (880.0, 0.12), (1175.0, 0.5))
        lead = sequence(notes, "sine", attack=0.006, tau=0.2)
        shimmer = sequence(notes, "triangle", attack=0.006, tau=0.14)
        return lowpass(mix(lead, scale(shimmer, 0.12)), 4000)

    arp = mix(
        bell(
            0.22,
            698.0,
            ratios=(1.0, 1.48, 2.31, 3.17),
            gains=(1.0, 0.4, 0.2, 0.1),
            attack=0.001,
            tau=0.08,
        ),
        offset(
            bell(
                0.22,
                880.0,
                ratios=(1.0, 1.48, 2.31, 3.17),
                gains=(1.0, 0.4, 0.2, 0.1),
                attack=0.001,
                tau=0.08,
            ),
            0.08,
        ),
        offset(
            bell(
                0.25,
                1047.0,
                ratios=(1.0, 1.48, 2.31, 3.17),
                gains=(1.0, 0.4, 0.2, 0.1),
                attack=0.001,
                tau=0.09,
            ),
            0.16,
        ),
        offset(
            bell(
                0.5,
                1397.0,
                ratios=(1.0, 1.48, 2.31, 3.17),
                gains=(1.0, 0.4, 0.2, 0.1),
                attack=0.001,
                tau=0.16,
            ),
            0.26,
        ),
    )
    air = bandpass(noise(0.3, rng, 0.01, 0.12), 2000, 8000)
    return lowpass(mix(scale(arp, 0.9), scale(air, 0.15)), 12_000)


def make_achievement(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        notes = ((1047.0, 0.08), (1319.0, 0.08), (1568.0, 0.08), (2093.0, 0.55))
        return bitcrush(
            sequence(notes, "square", duty=0.25, attack=0.001, tau=0.09),
            bits=7,
            hold=2,
        )

    if style == "modern":
        chime = bell(
            0.8,
            1047.0,
            ratios=(1.0, 2.01, 3.02, 4.1),
            gains=(1.0, 0.35, 0.18, 0.1),
            attack=0.002,
            tau=0.25,
        )
        sparkle = bell(
            0.5,
            1568.0,
            ratios=(1.0, 2.01, 3.02),
            gains=(1.0, 0.3, 0.15),
            attack=0.002,
            tau=0.16,
        )
        air = highpass(noise(0.35, rng, 0.005, 0.14), 5000)
        return lowpass(
            softclip(
                mix(
                    scale(chime, 0.8),
                    scale(offset(sparkle, 0.08), 0.5),
                    scale(air, 0.1),
                ),
                1.1,
            ),
            12_000,
        )

    if style == "soft":
        chime = bell(
            0.8,
            880.0,
            ratios=(1.0, 2.0, 3.0),
            gains=(1.0, 0.25, 0.1),
            attack=0.006,
            tau=0.25,
        )
        sparkle = bell(
            0.5, 1319.0, ratios=(1.0, 2.0), gains=(1.0, 0.2), attack=0.006, tau=0.16
        )
        return lowpass(mix(chime, scale(offset(sparkle, 0.1), 0.4)), 4500)

    chime = bell(
        0.85,
        1175.0,
        ratios=(1.0, 1.48, 2.31, 3.17),
        gains=(1.0, 0.4, 0.2, 0.1),
        attack=0.001,
        tau=0.25,
    )
    sparkle = bell(
        0.6,
        1760.0,
        ratios=(1.0, 1.41, 2.19, 3.09),
        gains=(1.0, 0.35, 0.18, 0.1),
        attack=0.001,
        tau=0.18,
    )
    air = bandpass(noise(0.4, rng, 0.003, 0.16), 2500, 9000)
    return lowpass(
        mix(scale(chime, 0.8), scale(offset(sparkle, 0.12), 0.5), scale(air, 0.12)),
        12_000,
    )


def make_shoot(style: str, rng: random.Random, variant: str) -> list[float]:
    number = int(variant)
    duration = 0.13 if number == 1 else 0.19
    f_start = 1700.0 if number == 1 else 1000.0
    f_end = 240.0 if number == 1 else 110.0

    if style == "retro":
        zap = tone(
            duration,
            f_start,
            f_end,
            "square",
            duty=0.25,
            attack=0.001,
            tau=duration * 0.45,
        )
        grit = tone(
            duration * 0.8, f_start * 1.5, f_end * 1.2, "saw", attack=0.001, tau=0.03
        )
        return bitcrush(mix(zap, scale(grit, 0.3)), bits=6, hold=2)

    if style == "modern":
        body = tone(duration, f_start, f_end, "saw", attack=0.0015, tau=duration * 0.5)
        sub = tone(
            duration,
            f_start * 0.25,
            f_end * 0.6,
            "sine",
            attack=0.002,
            tau=duration * 0.6,
        )
        tick = highpass(noise(0.03, rng, 0.0005, 0.008), 2500)
        return lowpass(
            softclip(mix(scale(body, 0.6), scale(sub, 0.5), scale(tick, 0.4)), 1.5),
            7000,
        )

    if style == "soft":
        body = tone(
            duration, f_start * 0.8, f_end, "sine", attack=0.004, tau=duration * 0.5
        )
        air = lowpass(noise(0.05, rng, 0.002, 0.02), 3000)
        return lowpass(mix(body, scale(air, 0.2)), 4500)

    body = tone(duration, f_start * 0.5, f_end, "triangle", attack=0.001, tau=0.05)
    zap = resonant_bandpass(noise(duration, rng, 0.001, 0.04), f_end * 1.6, q=2.5)
    air = moving_lowpass(noise(duration, rng, 0.002, 0.05), 6000, 900)
    return lowpass(mix(scale(body, 0.4), scale(zap, 0.8), scale(air, 0.5)), 8000)


def make_explosion(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        duration = 0.55
        blast = noise(duration, rng, 0.002, 0.16)
        sub = tone(duration, 130.0, 40.0, "square", duty=0.5, attack=0.001, tau=0.14)
        crack = noise(0.05, rng, 0.0005, 0.012)
        return bitcrush(mix(scale(blast, 0.8), sub, scale(crack, 0.5)), bits=5, hold=3)

    if style == "modern":
        duration = 0.8
        blast = moving_lowpass(noise(duration, rng, 0.002, 0.22), 7000, 350)
        sub = tone(duration, 95.0, 32.0, "sine", attack=0.001, tau=0.3)
        crack = highpass(noise(0.06, rng, 0.0005, 0.01), 2000)
        return lowpass(
            softclip(mix(blast, scale(sub, 0.8), scale(crack, 0.4)), 1.8), 8000
        )

    if style == "soft":
        duration = 0.6
        blast = lowpass(noise(duration, rng, 0.004, 0.18), 1400)
        sub = tone(duration, 80.0, 40.0, "sine", attack=0.004, tau=0.22)
        return lowpass(mix(blast, scale(sub, 0.6)), 2500)

    duration = 0.7
    blast = bandpass(noise(duration, rng, 0.002, 0.2), 180, 4000)
    sub = tone(0.4, 100.0, 35.0, "sine", attack=0.001, tau=0.12)
    debris = bandpass(noise(0.5, rng, 0.004, 0.16), 600, 3200)
    rumble = resonant_bandpass(noise(duration, rng, 0.003, 0.25), 85.0, q=1.2)
    return lowpass(
        mix(scale(blast, 0.9), scale(sub, 0.8), scale(debris, 0.3), scale(rumble, 0.5)),
        7000,
    )


def make_powerup(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        notes = (
            (440.0, 0.07),
            (554.0, 0.07),
            (659.0, 0.07),
            (880.0, 0.07),
            (1109.0, 0.18),
        )
        return bitcrush(
            sequence(notes, "square", duty=0.25, attack=0.001, tau=0.09),
            bits=7,
            hold=2,
        )

    if style == "modern":
        duration = 0.6
        sweep = tone(duration, 280.0, 1200.0, "saw", attack=0.02, tau=0.3)
        shimmer = sequence(
            ((880.0, 0.08), (1175.0, 0.08), (1568.0, 0.2)),
            "triangle",
            attack=0.005,
            tau=0.12,
        )
        sub = tone(0.4, 70.0, 160.0, "sine", attack=0.01, tau=0.25)
        return lowpass(
            softclip(mix(scale(sweep, 0.5), scale(shimmer, 0.4), scale(sub, 0.3)), 1.2),
            8000,
        )

    if style == "soft":
        duration = 0.7
        sweep = tone(duration, 330.0, 990.0, "sine", attack=0.03, tau=0.35)
        shimmer = sequence(
            ((660.0, 0.12), (990.0, 0.22)), "triangle", attack=0.01, tau=0.16
        )
        return lowpass(mix(sweep, scale(shimmer, 0.15)), 3800)

    duration = 0.6
    env = swell_env(samples_for(duration), 0.6)
    rise = moving_lowpass(noise(duration, rng, env=env), 350, 5000)
    chime = mix(
        bell(
            0.4,
            587.0,
            ratios=(1.0, 1.48, 2.31, 3.17),
            gains=(1.0, 0.4, 0.2, 0.1),
            attack=0.001,
            tau=0.12,
        ),
        offset(
            bell(
                0.45,
                880.0,
                ratios=(1.0, 1.48, 2.31, 3.17),
                gains=(1.0, 0.4, 0.2, 0.1),
                attack=0.001,
                tau=0.14,
            ),
            0.14,
        ),
    )
    strike = bandpass(noise(0.03, rng, 0.0005, 0.008), 2000, 9000)
    return lowpass(mix(scale(rise, 0.55), scale(chime, 0.5), scale(strike, 0.2)), 9000)


def make_shield(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        body = tone(0.28, 320.0, 300.0, "square", duty=0.25, attack=0.001, tau=0.09)
        ring = bell(
            0.3,
            960.0,
            ratios=(1.0, 2.7, 5.1),
            gains=(1.0, 0.35, 0.15),
            attack=0.001,
            tau=0.07,
        )
        strike = noise(0.03, rng, 0.0005, 0.008)
        return bitcrush(
            mix(scale(body, 0.6), scale(ring, 0.4), scale(strike, 0.5)),
            bits=6,
            hold=2,
        )

    if style == "modern":
        ring = bell(
            0.5,
            420.0,
            ratios=(1.0, 1.48, 2.76, 5.4),
            gains=(1.0, 0.5, 0.3, 0.15),
            attack=0.001,
            tau=0.14,
        )
        body = tone(0.3, 180.0, 140.0, "saw", attack=0.001, tau=0.08)
        strike = highpass(noise(0.05, rng, 0.0005, 0.01), 1800)
        return lowpass(
            softclip(mix(scale(ring, 0.8), scale(body, 0.3), scale(strike, 0.4)), 1.3),
            10_000,
        )

    if style == "soft":
        ring = bell(
            0.55,
            520.0,
            ratios=(1.0, 2.01, 3.0),
            gains=(1.0, 0.25, 0.12),
            attack=0.004,
            tau=0.18,
        )
        return lowpass(ring, 4500)

    ring = resonant_bandpass(noise(0.4, rng, 0.001, 0.16), 2300.0, q=7.0)
    body = tone(0.25, 160.0, 130.0, "sine", attack=0.001, tau=0.07)
    strike = bandpass(noise(0.05, rng, 0.0005, 0.012), 1500, 8000)
    return lowpass(mix(scale(ring, 0.9), scale(body, 0.5), scale(strike, 0.5)), 11_000)


def make_dash(style: str, rng: random.Random) -> list[float]:
    duration = 0.18
    env = swell_env(samples_for(duration), 0.45, 1.6)
    raw = noise(duration, rng, env=env)
    whoosh = moving_lowpass(bandpass(raw, 260, 2000), 400, 3000)
    body = tone(0.1, 120.0, 60.0, "sine", attack=0.002, tau=0.05)

    if style == "retro":
        tonal = tone(duration, 200.0, 520.0, "square", duty=0.25, env=env)
        return bitcrush(
            mix(scale(whoosh, 0.9), scale(tonal, 0.25), scale(body, 0.3)),
            bits=7,
            hold=2,
        )

    if style == "modern":
        tonal = tone(duration, 180.0, 480.0, "saw", env=env)
        return lowpass(
            softclip(mix(scale(whoosh, 1.0), scale(tonal, 0.3), scale(body, 0.4)), 1.2),
            4000,
        )

    if style == "soft":
        tonal = tone(duration, 240.0, 420.0, "sine", env=env)
        return lowpass(
            mix(scale(whoosh, 0.8), scale(tonal, 0.15), scale(body, 0.3)), 3000
        )

    return lowpass(
        mix(scale(whoosh, 1.2), bandpass(raw, 150, 800), scale(body, 0.5)), 6000
    )


def make_heal(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        notes = ((659.0, 0.09), (880.0, 0.09), (1175.0, 0.16))
        return bitcrush(
            sequence(notes, "square", duty=0.25, attack=0.002, tau=0.12),
            bits=7,
            hold=2,
        )

    if style == "modern":
        first = bell(
            0.5,
            659.0,
            ratios=(1.0, 2.0, 3.01),
            gains=(1.0, 0.3, 0.12),
            attack=0.004,
            tau=0.2,
        )
        second = bell(
            0.5,
            988.0,
            ratios=(1.0, 2.0, 3.01),
            gains=(1.0, 0.3, 0.12),
            attack=0.004,
            tau=0.2,
        )
        air = highpass(noise(0.4, rng, 0.01, 0.18), 3000)
        return lowpass(mix(first, offset(second, 0.1), scale(air, 0.08)), 8000)

    if style == "soft":
        first = bell(
            0.55,
            659.0,
            ratios=(1.0, 2.0, 3.0),
            gains=(1.0, 0.2, 0.08),
            attack=0.008,
            tau=0.24,
        )
        second = bell(
            0.5,
            988.0,
            ratios=(1.0, 2.0),
            gains=(1.0, 0.15),
            attack=0.008,
            tau=0.2,
        )
        return lowpass(mix(first, scale(offset(second, 0.12), 0.8)), 3800)

    first = bell(
        0.45,
        784.0,
        ratios=(1.0, 1.48, 2.31, 3.17),
        gains=(1.0, 0.4, 0.2, 0.1),
        attack=0.003,
        tau=0.16,
    )
    breath = bandpass(noise(0.35, rng, 0.02, 0.14), 1200, 5000)
    return lowpass(mix(first, scale(breath, 0.18)), 9000)


def make_enemy_alert(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        notes = ((880.0, 0.09), (660.0, 0.09), (880.0, 0.12))
        return bitcrush(
            sequence(notes, "square", duty=0.5, attack=0.001, tau=0.08),
            bits=6,
            hold=2,
        )

    if style == "modern":
        high = tone(0.09, 880.0, 860.0, "saw", attack=0.002, tau=0.04)
        detune = tone(0.09, 890.0, 870.0, "saw", attack=0.002, tau=0.04)
        low = tone(0.13, 660.0, 640.0, "saw", attack=0.002, tau=0.05)
        body = mix(
            scale(high, 0.35),
            scale(detune, 0.25),
            offset(scale(low, 0.55), 0.1),
        )
        return lowpass(softclip(body, 1.4), 5000)

    if style == "soft":
        notes = ((784.0, 0.12), (587.0, 0.16))
        return lowpass(sequence(notes, "sine", attack=0.006, tau=0.1), 3200)

    ping = bell(
        0.32,
        1245.0,
        ratios=(1.0, 2.4, 4.1),
        gains=(1.0, 0.3, 0.15),
        attack=0.001,
        tau=0.09,
    )
    answer = bell(
        0.3,
        830.0,
        ratios=(1.0, 2.4, 4.1),
        gains=(1.0, 0.3, 0.15),
        attack=0.001,
        tau=0.08,
    )
    strike = bandpass(noise(0.04, rng, 0.0005, 0.01), 2000, 7000)
    return lowpass(mix(ping, offset(answer, 0.14), scale(strike, 0.3)), 9000)


def make_splash(style: str, rng: random.Random) -> list[float]:
    if style == "retro":
        duration = 0.35
        blast = bitcrush(noise(duration, rng, 0.002, 0.09), bits=6, hold=3)
        drop = tone(0.16, 420.0, 140.0, "square", duty=0.5, attack=0.001, tau=0.05)
        return mix(scale(blast, 0.7), drop)

    if style == "modern":
        duration = 0.4
        water = moving_lowpass(noise(duration, rng, 0.004, 0.14), 6000, 500)
        sub = tone(0.3, 160.0, 55.0, "sine", attack=0.002, tau=0.1)
        return lowpass(softclip(mix(water, scale(sub, 0.5)), 1.2), 9000)

    if style == "soft":
        duration = 0.42
        water = lowpass(noise(duration, rng, 0.01, 0.16), 1600)
        sub = tone(0.28, 120.0, 60.0, "sine", attack=0.006, tau=0.12)
        return lowpass(mix(water, scale(sub, 0.5)), 2600)

    duration = 0.42
    water = moving_lowpass(
        bandpass(noise(duration, rng, 0.003, 0.15), 300, 6000), 5000, 500
    )
    droplet = bell(
        0.2,
        1100.0,
        ratios=(1.0, 2.2, 3.9),
        gains=(1.0, 0.3, 0.12),
        attack=0.001,
        tau=0.05,
    )
    return lowpass(mix(scale(water, 0.9), scale(droplet, 0.25)), 9000)


def build_sound(style: str, kind: str, variant: str, rng: random.Random) -> list[float]:
    if kind == "jump":
        return make_jump(style, rng, variant)
    if kind == "walk":
        return make_walk(style, rng, variant)
    if kind == "hit":
        return make_hit(style, rng, variant)
    if kind == "land":
        return make_land(style, rng, variant)
    if kind == "swing":
        return make_swing(style, rng, variant)
    if kind == "hurt":
        return make_hurt(style, rng, variant)
    if kind == "death":
        return make_death(style, rng)
    if kind == "pickup":
        return make_pickup(style, rng, variant)
    if kind.startswith("ui_"):
        return make_ui(style, rng, kind)
    if kind == "victory":
        return make_victory(style, rng)
    if kind == "defeat":
        return make_defeat(style, rng)
    if kind == "level_up":
        return make_level_up(style, rng)
    if kind == "achievement":
        return make_achievement(style, rng)
    if kind == "shoot":
        return make_shoot(style, rng, variant)
    if kind == "explosion":
        return make_explosion(style, rng)
    if kind == "powerup":
        return make_powerup(style, rng)
    if kind == "shield":
        return make_shield(style, rng)
    if kind == "dash":
        return make_dash(style, rng)
    if kind == "heal":
        return make_heal(style, rng)
    if kind == "enemy_alert":
        return make_enemy_alert(style, rng)
    if kind == "splash":
        return make_splash(style, rng)
    raise ValueError(f"unknown sound kind: {kind}")


# ---------------------------------------------------------------------------
# Rendering, encoding, and verification
# ---------------------------------------------------------------------------


def apply_fades(
    layer: list[float], fade_in: float = 0.003, fade_out: float = 0.012
) -> list[float]:
    n = len(layer)
    fade_in_samples = min(n, samples_for(fade_in))
    fade_out_samples = min(n, samples_for(fade_out))

    if fade_in_samples > 1:
        for index in range(fade_in_samples):
            layer[index] *= 0.5 - 0.5 * math.cos(
                math.pi * index / (fade_in_samples - 1)
            )
    if fade_out_samples > 1:
        for index in range(fade_out_samples):
            layer[n - 1 - index] *= 0.5 - 0.5 * math.cos(
                math.pi * index / (fade_out_samples - 1)
            )
    return layer


def finalize(layer: list[float], db: float = -6.0) -> list[float]:
    """Remove DC, fade edges, and peak-normalize to target db dBFS."""
    if not layer:
        return layer
    mean = sum(layer) / len(layer)
    layer = [value - mean for value in layer]
    apply_fades(layer)
    peak = max(abs(value) for value in layer)
    if peak > 0.0:
        gain = 10 ** (db / 20.0) / peak
        layer = [value * gain for value in layer]
    return layer


def write_wav(path: Path, layer: Sequence[float]) -> None:
    pcm = array(
        "h",
        (max(-32768, min(32767, round(value * 32767))) for value in layer),
    )
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())


def read_wav(path: Path) -> list[float]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"{path}: expected mono 16-bit WAV")
        if handle.getframerate() != SAMPLE_RATE:
            raise ValueError(
                f"{path}: expected {SAMPLE_RATE} Hz, got {handle.getframerate()}"
            )
        raw = handle.readframes(handle.getnframes())
    pcm = array("h")
    pcm.frombytes(raw)
    if sys.byteorder != "little":
        pcm.byteswap()
    return [value / 32768.0 for value in pcm]


def encode(wav_path: Path, fmt: str, destination: Path) -> None:
    if fmt == "ogg":
        codec = ["-c:a", "libvorbis", "-q:a", "5"]
    elif fmt == "mp3":
        codec = ["-c:a", "libmp3lame", "-b:a", "192k"]
    else:
        raise ValueError(f"unsupported encoded format: {fmt}")

    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        *codec,
        str(destination),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or f"ffmpeg exited with {result.returncode}"
        )
    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError("encoder produced no output")


def decibels(amplitude: float) -> float:
    return 20.0 * math.log10(amplitude) if amplitude > 0.0 else float("-inf")


def generate(
    out_root: Path,
    styles: Sequence[str],
    formats: Sequence[str],
    seed: int,
    db: float = -6.0,
) -> list[dict]:
    ffmpeg = shutil.which("ffmpeg")
    encoded_formats = [fmt for fmt in formats if fmt != "wav"]
    if encoded_formats and not ffmpeg:
        print("warning: ffmpeg not found; writing WAV masters only", file=sys.stderr)
        encoded_formats = []

    out_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    total = len(styles) * len(SOUNDS)
    done = 0

    for style_index, style in enumerate(STYLES):
        if style not in styles:
            continue
        style_dir = out_root / style
        style_dir.mkdir(parents=True, exist_ok=True)

        for sound_index, (kind, variant) in enumerate(SOUNDS):
            name = f"{kind}_{variant}"
            sound_seed = seed + style_index * 1009 + sound_index * 97
            rng = random.Random(sound_seed)
            layer = finalize(build_sound(style, kind, variant, rng), db=db)

            wav_path = style_dir / f"{name}.wav"
            write_wav(wav_path, layer)
            files = [str(wav_path.relative_to(out_root))]

            for fmt in encoded_formats:
                destination = style_dir / f"{name}.{fmt}"
                try:
                    encode(wav_path, fmt, destination)
                    files.append(str(destination.relative_to(out_root)))
                except Exception as error:  # noqa: BLE001 - keep generating the pack
                    print(
                        f"warning: could not encode {style}/{name}.{fmt}: {error}",
                        file=sys.stderr,
                    )

            peak = max(abs(value) for value in layer)
            rms = math.sqrt(sum(value * value for value in layer) / len(layer))
            entries.append(
                {
                    "style": style,
                    "name": name,
                    "kind": kind,
                    "variant": variant,
                    "duration_seconds": round(len(layer) / SAMPLE_RATE, 4),
                    "peak_dbfs": round(decibels(peak), 2),
                    "rms_dbfs": round(decibels(rms), 2),
                    "seed": sound_seed,
                    "files": files,
                }
            )

            done += 1
            print(
                f"[{done:>3}/{total}] {style}/{name:<12} {len(layer) / SAMPLE_RATE:5.2f}s"
            )

    manifest = {
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "bits_per_sample": 16,
        "db": db,
        "base_seed": seed,
        "styles": list(styles),
        "sounds": entries,
    }
    (out_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return entries


def check_outputs(out_root: Path) -> int:
    wav_files = sorted(out_root.glob("*/*.wav"))
    if not wav_files:
        print(f"no WAV files found under {out_root}", file=sys.stderr)
        return 1

    print(f"\n{'file':<30} {'duration':>8} {'peak dBFS':>10} {'rms dBFS':>9}")
    print("-" * 61)

    failures: list[str] = []
    for path in wav_files:
        try:
            layer = read_wav(path)
        except ValueError as error:
            print(f"FAIL {error}", file=sys.stderr)
            failures.append(str(path))
            continue

        peak = max(abs(value) for value in layer)
        rms = math.sqrt(sum(value * value for value in layer) / len(layer))
        peak_dbfs = decibels(peak)

        relative = path.relative_to(out_root)
        print(
            f"{relative!s:<30} {len(layer) / SAMPLE_RATE:7.2f}s {peak_dbfs:9.2f} {decibels(rms):8.2f}"
        )

        if peak_dbfs > -0.5:
            failures.append(f"{relative}: peak {peak_dbfs:.2f} dBFS is too hot")
        if not layer:
            failures.append(f"{relative}: empty audio")

    print("-" * 61)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    print(f"OK: {len(wav_files)} WAV files, all within peak target")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate procedural game sound effects (stdlib only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("sounds"),
        help="output directory",
    )
    parser.add_argument(
        "--style",
        choices=("all", *STYLES),
        default="all",
        help="style to render",
    )
    parser.add_argument(
        "--format",
        choices=("all", *FORMATS),
        default="all",
        help="additional format to produce; WAV masters are always written",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=BASE_SEED,
        help="base random seed for reproducible variation",
    )
    parser.add_argument(
        "--db",
        type=float,
        default=-6.0,
        metavar="DBFS",
        help="target peak level in dBFS for all rendered sounds; default -6.0",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify rendered WAV files after generating",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="verify existing WAV files without generating",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    if args.check_only:
        return check_outputs(args.out)

    styles = STYLES if args.style == "all" else (args.style,)
    formats = FORMATS if args.format == "all" else ("wav", args.format)

    print(
        f"Rendering {len(styles) * len(SOUNDS)} sounds "
        f"({', '.join(styles)}) to {args.out}/ ..."
    )
    generate(args.out, styles, formats, args.seed, args.db)
    print(f"\nWrote {args.out}/manifest.json")

    if args.check:
        return check_outputs(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
