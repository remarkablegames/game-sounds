# Game Sounds

🎵 Procedurally generated game sound effects in four audio styles.

Audio synthesized using the Python standard library (no samples, NumPy, or SciPy). Each sound is rendered as a 44.1 kHz, mono WAV master. Optional OGG and MP3 versions require FFmpeg.

## Requirements

- [Python 3.9+](https://www.python.org/). Synthesis and WAV generation use the standard library; no Python packages or audio samples are needed.
- [FFmpeg](https://ffmpeg.org/) with libvorbis and libmp3lame is required only for OGG and MP3 output.

## Install

Clone the repository:

```sh
git clone https://github.com/remarkablegames/gravitate.git
cd gravitate
```

## Generate

Run these commands from the project directory, where `generate.py` is located. Generated files are written under `sounds/`.

Generate the full pack, including WAV, OGG, and MP3:

```sh
python3 generate.py
```

Generate one style as WAV only:

```sh
python3 generate.py --style retro --format wav
```

Generate WAV and OGG:

```sh
python3 generate.py --format ogg
```

Use a specific random seed:

```sh
python3 generate.py --seed 42
```

The same seed produces the same rendered WAV audio when run with the same generator version and settings.

WAV masters are always written. The `--format` option controls compressed copies: use `ogg` for WAV plus OGG, or `wav` for WAV only. If FFmpeg or a required codec is unavailable, compressed-file generation may fail; WAV masters are still written.

## Verify

Check existing WAV files without regenerating them:

```sh
python3 generate.py --check
```

Regenerate the sounds and then verify the results:

```sh
python3 generate.py --check-only
```

Verification checks every WAV file under `sounds/` for mono, 16-bit, 44.1 kHz audio. It reports each file's duration, peak level, and RMS level, and exits with a nonzero status if a file is malformed, empty, or exceeds −0.5 dBFS.

## Styles

4 styles:

| Style | Character |
| --- | --- |
| `retro` | Chiptune pulse and square leads, bitcrush/sample-hold grit, arcade sweeps |
| `modern` | Detuned saw layers, sub-bass thumps, resonant transients, soft clipping |
| `soft` | Sine/triangle tones, gentle attacks, low-passed noise; muted and non-fatiguing |
| `realistic` | Noise-based scuffs and impacts, resonant band-passes, pitch-dropped thumps |

## Sounds

Each style includes 18 sounds:

| Category | Files | Variants | Use |
| --- | --- | --- | --- |
| Jump | `jump_01`, `jump_02` | 2 | Leaving the ground |
| Walk | `walk_01` … `walk_04` | 4 | Rotate for footsteps |
| Hit | `hit_light`, `hit_medium`, `hit_heavy` | 3 | Light, medium, and heavy impacts |
| Land | `land_01`, `land_02` | 2 | Touching down after a jump |
| Swing | `swing_01`, `swing_02` | 2 | Attack whooshes |
| Hurt | `hurt_01`, `hurt_02` | 2 | Player damage |
| Death | `death_01` | 1 | Death / game-over stinger |
| Pickup | `pickup_01`, `pickup_02` | 2 | Coins, items, collectibles |

## Audio Specs

- 44,100 Hz, mono, 16-bit PCM WAV
- Peak normalized to −1 dBFS (no clipping); verification allows peaks up to −0.5 dBFS
- DC offset removed
- 3 ms fade-in and 12 ms fade-out per sound
- OGG Vorbis quality 5; MP3 at 192 kbps

## Layout

```
sounds/
├── manifest.json # metadata for the most recent generation
├── retro/
│   ├── jump_01.wav / .ogg / .mp3
│   ├── walk_01.wav / .ogg / .mp3
│   ├── hit_light.wav / .ogg / .mp3
│   └── ...
├── modern/
├── soft/
└── realistic/
```

Compressed files are present only when generated using the relevant output format.

## Manifest

`sounds/manifest.json` records, for each rendered sound:

- Style, name, kind, and variant
- Duration, peak level in dBFS, and RMS level in dBFS
- The random seed used
- Relative paths to the WAV and any generated OGG or MP3 files

The manifest records metadata for the most recent generation run. If you generate a subset (e.g., `retro`), the manifest reflects that subset.

## License

[LICENSE](LICENSE)
