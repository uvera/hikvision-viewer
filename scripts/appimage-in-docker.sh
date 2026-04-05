#!/usr/bin/env bash
# Docker entry: install build deps, copy sources to a writable tree, run appimage-bundle.sh.
# Expects project mounted read-only at /src and output dir at /out (see build-appimage.sh).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends rsync

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
rsync -a /src/ "$work/src/" \
	--exclude .git/ \
	--exclude .venv/ \
	--exclude dist/ \
	--exclude build/ \
	--exclude AppDir/

chmod +x "$work/src/scripts/appimage-bundle.sh" 2>/dev/null || true

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq --no-install-recommends \
	binutils ca-certificates curl desktop-file-utils file fuse imagemagick libpython3.10 \
	patchelf python3-pip python3-venv \
	libmpv1 libgl1 libglib2.0-0 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 \
	libxcb-image0 libxcb-keysyms1 libxcb-render0 libxcb-render-util0 \
	libxcb-shape0 libxcb-shm0 libxcb-util1 libxcb-xfixes0 libxcb-xinerama0 \
	libxcb-xkb1 libxcb1 libx11-xcb1 libfontconfig1 libfreetype6 libdbus-1-3 \
	libegl1 libx11-6

cd "$work/src"
python3 -m pip install -q --upgrade pip
python3 -m pip install -q pyinstaller
python3 -m pip install -q .

APPIMAGE_OUTPUT_DIR=/out ./scripts/appimage-bundle.sh
if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
	chown "$HOST_UID:$HOST_GID" /out/hikvision-viewer-*-x86_64.AppImage 2>/dev/null || true
fi
