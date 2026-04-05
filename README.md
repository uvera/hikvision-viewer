# Hikvision viewer

Desktop viewer for **live RTSP streams** from IP cameras, with a focus on **Hikvision-style URLs** (`/Streaming/Channels/101` main stream, `102` sub stream). The UI is built with **PyQt6**; video is handled by **mpv** (embedded windows or one mpv subprocess per tile).

## Goals

- **Today:** Reliable multi-camera **live viewing**—grid layout, single-camera mode with prev/next, fullscreen, and YAML-based configuration with optional secret management.
- **Future:** **Recorded playback** (for example NVR timelines or file-based review) is not implemented yet; it is a planned direction for the project.

The in-app configuration tab labeled **Playback** adjusts **mpv and display behavior** (hardware decode, video output, Wayland embedding, dark theme, single-view camera order)—not playback of stored recordings.

## Features

- **Grid** and **Single** view; keyboard shortcuts (arrow keys, `G` to toggle view, `F11` fullscreen, `Esc` to leave fullscreen).
- **YAML config** under the XDG config directory (default `~/.config/hikvision-viewer/config.yaml`), with `config.example.yaml` as a template.
- **Hikvision URL helper** in the configuration editor, plus arbitrary RTSP/custom URLs.
- **`{ENV_VAR}` placeholders** in URLs, loaded from **encrypted `.env.enc`** (OS keyring holds the encryption key).
- **Reload config** from the UI without editing files by hand.

## Requirements

- **Python** 3.10+
- **mpv** installed on the system (the `python-mpv` package talks to libmpv)

## Install

From a checkout:

```bash
pip install -e .
```

Or install dependencies from `requirements.txt` / `pyproject.toml` and run the package’s `main` as appropriate.

## Run

```bash
hikvision-viewer
```

If no config exists yet, copy `config.example.yaml` to `~/.config/hikvision-viewer/config.yaml` (or place `config.yaml` next to your checkout for development) and set your stream URLs and environment variables.

## Configuration notes

See comments in **`config.example.yaml`** for:

- Optional **`viewer:`** block (mpv subprocess, `hwdec`, `vo`, Qt/Wayland, forced dark mode, single-view stream order).
- **`HIKVISION_*` environment variables** that override YAML when set to a non-empty value before startup.

A **Debian package** layout lives under `debian/` for system installs with packaged Python dependencies and `mpv`.

## License

See `LICENSE`.
