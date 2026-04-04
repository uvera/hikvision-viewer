#!/usr/bin/env bash
# Build dist/hikvision-viewer-<version>.tar.gz (clean tree for makepkg/AUR),
# copy it to aur/, refresh sha256sums in PKGBUILD, and run python -m build (wheel + sdist).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY=python3
fi

VERSION="$("$PY" -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")"
NAME="hikvision-viewer-${VERSION}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$ROOT/dist"

rsync -a \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.egg-info' \
  --exclude='dist' \
  --exclude='build' \
  --exclude='.env' \
  --exclude='config.yaml' \
  --exclude='*.pyc' \
  --exclude='.mypy_cache' \
  --exclude='.ruff_cache' \
  --exclude='.cursor' \
  --exclude="${NAME}.tar.gz" \
  --exclude='aur/src' \
  --exclude='aur/pkg' \
  --exclude='*.pkg.tar.zst' \
  --exclude='*.pkg.tar.xz' \
  "$ROOT/" "$TMP/$NAME/"

tar -C "$TMP" -czf "$ROOT/dist/${NAME}.tar.gz" "$NAME"
cp -f "$ROOT/dist/${NAME}.tar.gz" "$ROOT/aur/${NAME}.tar.gz"

SUM="$(sha256sum "$ROOT/dist/${NAME}.tar.gz" | awk '{print $1}')"
perl -i -pe "s/^sha256sums=\([^)]*\)/sha256sums=('${SUM}')/" "$ROOT/aur/PKGBUILD"

"$PY" -m pip install -q build setuptools wheel 2>/dev/null || true
"$PY" -m build --wheel --sdist --no-isolation

if command -v makepkg >/dev/null 2>&1; then
  (cd "$ROOT/aur" && makepkg --printsrcinfo >"$ROOT/aur/.SRCINFO") || true
fi

echo "Source tarball: dist/${NAME}.tar.gz  (copy in aur/ for makepkg)"
echo "Wheel / sdist:  dist/"
echo "SHA256:         ${SUM}"
