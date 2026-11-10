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
