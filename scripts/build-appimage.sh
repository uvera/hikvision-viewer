#!/usr/bin/env bash
# Build an x86_64 AppImage (PyInstaller + linuxdeploy). Uses Docker by default
# for a reproducible glibc baseline (Ubuntu 22.04). Set APPIMAGE_USE_DOCKER=0
# to run scripts/appimage-bundle.sh on the host (install build deps yourself).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out_dir="${APPIMAGE_OUTPUT_DIR:-$root/dist}"
mkdir -p "$out_dir"

if [[ "${APPIMAGE_USE_DOCKER:-1}" == "1" ]]; then
	if ! command -v docker >/dev/null 2>&1; then
		echo "Docker is required (set APPIMAGE_USE_DOCKER=0 to build on the host)." >&2
		exit 1
	fi
	image="${APPIMAGE_BUILD_IMAGE:-ubuntu:22.04}"
	docker run --rm \
		-e "HOST_UID=$(id -u)" \
		-e "HOST_GID=$(id -g)" \
		-v "$root:/src:ro" \
		-v "$out_dir:/out" \
		"$image" \
		bash /src/scripts/appimage-in-docker.sh
	echo "Artifacts under: $out_dir"
	exit 0
fi

if ! command -v pyinstaller >/dev/null 2>&1; then
	echo "Install pyinstaller and system deps, or use Docker (default)." >&2
	exit 1
fi

python3 -m pip install -q pyinstaller
python3 -m pip install -q "$root"
APPIMAGE_OUTPUT_DIR="$out_dir" "$root/scripts/appimage-bundle.sh"
echo "Artifacts under: $out_dir"
