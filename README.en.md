# RealEyes

> 🚧 **Work in Progress** — APIs, config and layout may change. Issues are welcome, but this is not yet recommended for production or security use.
>
> English | [简体中文](README.md)

RealEyes is a **24/7 on-box "computer eye"**: it watches the screen, windows, camera, video streams, files and system audio, judges anomalies with **local vision models**, and only records events / writes digests / sends desktop notifications when something matters — **zero cloud API tokens by default**.

## Features

- **Local-first, zero-token default**: L0 rules + local Qwen VLM behind an OpenAI-compatible gateway (llama.cpp). Cloud is an optional, budget-gated tier.
- **Multi-source capture**: screen, window/process capture, camera, local video, network streams, phone over ADB (interface reserved).
- **Four escalation tiers**: L0 rules → L1 small VLM → L2 larger local model → L3 cloud (off by default).
- **Parallel attention**: multiple workers and a model pool, built for fast-changing scenes such as games and live streams.
- **Events & memory**: JSONL + SQLite + keyframes, hourly/daily digests, keyword `--recall`.
- **Non-face identity baseline**: idle input + foreground app to infer presence; OSNet body Re-ID reserved.
- **Extra senses**: file watching, audio loudness ("ears"), selftests and crash supervision.

## Architecture

| Tier | What it does | Runs on | Typical latency |
|---|---|---|---|
| L0 | Motion, blackout/occlusion, frozen-screen rules | CPU (OpenCV, no model) | milliseconds |
| L1 | Keyframe → local small VLM, structured verdict | Local gateway + qwen3.5-9b | ~1–3 s |
| L2 | Hard/risky events → larger local model | Local gateway + qwen3.8-27b / 35B-A3B | seconds |
| L3 | Cloud model, bounded by daily budget/warning lines | Any OpenAI-compatible service | network-bound |

Everything happens on the local machine; L3 is off unless explicitly configured with an API key.

## Quick start

Requirements: Windows 10/11, Python 3.10+, ffmpeg, and a local OpenAI-compatible vision model service (see Dependencies).

```powershell
cd src
python realeyes.py --selftest      # 8 checks, no camera/GPU needed
python realeyes.py --check         # grab one frame and ask the local VLM
python realeyes.py --duration 60   # run 60s in the foreground
python realeyes.py --recall keyword --hours 24
powershell -File start_realeyes.ps1     # hidden background (checks gateway)
powershell -File supervise_realeyes.ps1 # auto-restart on crash (use for login autostart)
```

Config lives in `src/config.yaml` (`service.data_root` defaults to `F:\realeyes`; point it at your own data directory). Runtime artifacts (events, keyframes, digests, logs, state) are generated locally and are not versioned.

## Dependencies (strong third-party requirements — read first)

RealEyes ships **no** model weights or inference framework. The default architecture requires a local inference service:

1. **Local LLM gateway (required)**: any OpenAI-compatible `/v1/chat/completions` server; recommended [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT) serving Qwen GGUF.
   - Recommended models: **Qwen3.5-9B** (vision, default L1), **Qwen3.8-27B** (vision, optional L2), **Qwen3.5-35B-A3B** (text-only, optional L2).
   - Weights: unsloth GGUF repos on Hugging Face / ModelScope; follow their licenses. Weights are never committed.
   - Reference implementation/config: `integrations/local-llm-gateway/`. If your gateway needs an API key, set `QWEN_LOCAL_API_KEY` (the bundled gateway ignores it; it is a placeholder).
2. **ffmpeg (required)**: decoding for windows/network streams/live sources; LGPL/GPL depending on build; must be on PATH.
3. **Python packages**: `opencv-python` (Apache-2.0), `numpy` (BSD), `mss` (MIT), `soundcard` (BSD), `PyYAML` (MIT); optional `onnxruntime` (MIT, for future YOLO/OSNet).
4. **Windows-specific**: DirectShow camera, ctypes Win32 calls (window rect/title, idle time); phone source needs `adb` (Android SDK, Apache-2.0) with USB debugging authorized.
5. **Optional integrations**: DeepSeek Harness (DSH) plugin `local_eyes` (screen/image → local VLM → text only), see `integrations/dsh-local-eyes/`; its capture/encode API comes from [dsh-see-now](https://github.com/SAOS-3624/dsh-see-now) (Apache-2.0). DSH profile wiring is in `integrations/dsh/`.

Model weights, third-party tools, test assets and comparison data are **not** distributed with this repository.

## Signal sources (`camera.source`)

```
0                        Local camera (DirectShow)
window:<title substring> Window/process capture (player, game, live window)
screen:x,y,w,h           Screen region
bilibili:<room_id>       Bilibili live stream (ffmpeg pull; subject to platform terms)
https://… / rtsp://…     Network stream
D:\videos\demo.mp4       Local video file (looped; for testing)
phone                    Phone screenshot over ADB (authorization required)
```

## Layout

```
src/           main service realeyes.py + identity/filewatch/memory/phone + config & control scripts
docs/          SPEC / PLAN / identity / memory / STATUS
integrations/  local LLM gateway, local_eyes plugin, DSH profile snippet & AGENTS example
scripts/       run.ps1, selftest.ps1
```

## Docs

- [`docs/SPEC.md`](docs/SPEC.md) — spec, event model, acceptance criteria
- [`docs/PLAN.md`](docs/PLAN.md) — milestones and deployment switch
- [`docs/identity.md`](docs/identity.md) — non-face identity design
- [`docs/memory.md`](docs/memory.md) — memory tiers and recall
- [`docs/STATUS.md`](docs/STATUS.md) — progress, environment, known issues

## Roadmap (WIP)

- [ ] Real camera motion pipeline and occlusion handling
- [ ] Live phone integration over ADB (screenshot stream / scrcpy)
- [ ] OSNet body Re-ID and richer non-face identity
- [ ] Automatic L2/L3 escalation with cloud budget gates
- [ ] Deployment scripts, service mode, config wizard

## Copyright & license

Code and design © 2026 JadeL-Kemo. Commercial redistribution requires permission.

This repository **does not** contain third-party copyrighted assets: model weights, test screenshots, test data and comparison tools are excluded and only referenced by link/description. Third-party dependencies (llama.cpp, Qwen, ffmpeg, Python packages, DSH, dsh-see-now, etc.) remain under their respective licenses and copyrights.
