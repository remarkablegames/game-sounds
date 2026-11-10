# Game Sounds

[![test](https://github.com/remarkablegames/game-sounds/actions/workflows/test.yml/badge.svg)](https://github.com/remarkablegames/game-sounds/actions/workflows/test.yml)

🎵 Procedurally generated game sound effects in 4 audio styles:

- [Demo](https://remarkablegames.org/game-sounds/)
- [Download](https://github.com/remarkablegames/game-sounds/releases/latest/download/sounds.zip)

Audio is synthesized using the Python standard library (no samples or dependencies). Each sound is rendered as a 44.1 kHz mono WAV master, with optional OGG and MP3 versions.

## Requirements

- [Python 3.10+](https://www.python.org/). Synthesis and WAV generation use the standard library; no Python packages or audio samples are needed.
- [FFmpeg](https://ffmpeg.org/) with `libvorbis` and `libmp3lame` is required only for OGG and MP3 output.

Clone the repository:

```sh
git clone https://github.com/remarkablegames/game-sounds.git
cd game-sounds
```

## Generate

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

> The same seed produces the same rendered WAV audio when run with the same generator version and settings.

Adjust the volume of all rendered sounds (target peak in dBFS; lower = quieter):

```sh
python3 generate.py --db -1
```

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

| Style | Description |
| --- | --- |
| _retro_ | Chiptune pulse and square leads, bitcrush/sample-hold grit, arcade sweeps |
| _modern_ | Detuned saw layers, sub-bass thumps, resonant transients, soft clipping |
| _soft_ | Sine/triangle tones, gentle attacks, low-passed noise; muted and non-fatiguing |
| _realistic_ | Noise-based scuffs and impacts, resonant band-passes, pitch-dropped thumps |

## Sounds

Each style includes 39 sounds:

| Category | Description | Sounds |
| --- | --- | --- |
| _Jump_ | Leaving the ground | `jump_01`, `jump_02` |
| _Walk_ | Rotate for footsteps | `walk_01`, `walk_02`, `walk_03`, `walk_04` |
| _Land_ | Touching down after a jump | `land_01`, `land_02` |
| _Dash_ | Quick dodge or sprint burst | `dash` |
| _Hit_ | Light, medium, and heavy impacts | `hit_light`, `hit_medium`, `hit_heavy` |
| _Swing_ | Attack whooshes | `swing_01`, `swing_02` |
| _Shoot_ | Projectiles and laser fire | `shoot_01`, `shoot_02` |
| _Explosion_ | Blasts and heavy impacts | `explosion` |
| _Shield_ | Block, guard, or deflect | `shield` |
| _Alert_ | Enemy spotted or warning | `enemy_alert` |
| _Pickup_ | Coins, items, collectibles | `pickup_01`, `pickup_02` |
| _Powerup_ | Rising buff or upgrade sweep | `powerup` |
| _Heal_ | Restoring health or healing magic | `heal` |
| _Splash_ | Water entry or splash | `splash` |
| _Hurt_ | Player damage | `hurt_01`, `hurt_02` |
| _Death_ | Death / game-over stinger | `death` |
| _UI_ | Menus, buttons, and alerts | `ui_click`, `ui_hover`, `ui_confirm`, `ui_back`, `ui_error`, `ui_open`, `ui_close`, `ui_notification` |
| _Victory_ | Win / level complete | `victory` |
| _Defeat_ | Loss / round over | `defeat` |
| _Level Up_ | Character or skill progression | `level_up` |
| _Achievement_ | Unlocks, trophies, and rewards | `achievement` |

## Audio Specs

- 44,100 Hz, mono, 16-bit PCM WAV
- Peak normalized to −6 dBFS by default (no clipping)
- DC offset removed
- 3 ms fade-in and 12 ms fade-out per sound
- OGG Vorbis quality 5; MP3 at 192 kbps

## Layout

```
sounds/
├── manifest.json # metadata for the latest generation
├── retro/
│   ├── jump_01.wav / .ogg / .mp3
│   ├── walk_01.wav / .ogg / .mp3
│   ├── hit_light.wav / .ogg / .mp3
│   └── ...
├── modern/
├── soft/
└── realistic/
```

## Manifest

`sounds/manifest.json` records, for each rendered sound:

- Style, name, kind, and variant
- Duration, peak level in dBFS, and RMS level in dBFS
- The random seed used
- Relative paths to the WAV and any generated OGG or MP3 files

The manifest records metadata for the most recent generation run. If you generate a subset (e.g., `retro`), the manifest reflects that subset.

## License

[MIT](LICENSE)
