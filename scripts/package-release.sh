#!/usr/bin/env bash
# Build dist/hikvision-viewer-<version>.tar.gz (clean tree), run python -m build.
#
# By default this also refreshes aur/ for local makepkg (tarball copy + PKGBUILD sha256 + .SRCINFO).
# Set SKIP_AUR=1 to only produce dist/ and leave aur/ unchanged.
#
# For published GitHub tags + AUR metadata pointing at the tag archive, use scripts/release-github-aur.sh
# (or SKIP_AUR_REFRESH=1 there if you only want the GitHub release without touching aur/).
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
  --exclude='.env.enc' \
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
SUM="$(sha256sum "$ROOT/dist/${NAME}.tar.gz" | awk '{print $1}')"

if [[ "${SKIP_AUR:-0}" != "1" ]]; then
  cp -f "$ROOT/dist/${NAME}.tar.gz" "$ROOT/aur/${NAME}.tar.gz"
  perl -i -pe "s/^sha256sums=\([^)]*\)/sha256sums=('${SUM}')/" "$ROOT/aur/PKGBUILD"
  if command -v makepkg >/dev/null 2>&1; then
    (cd "$ROOT/aur" && makepkg --printsrcinfo >"$ROOT/aur/.SRCINFO") || true
  fi
fi

"$PY" -m pip install -q build setuptools wheel 2>/dev/null || true
"$PY" -m build --wheel --sdist --no-isolation

echo "Source tarball: dist/${NAME}.tar.gz"
if [[ "${SKIP_AUR:-0}" == "1" ]]; then
  echo "AUR:            skipped (SKIP_AUR=1; aur/ unchanged)"
else
  echo "AUR:            aur/${NAME}.tar.gz + PKGBUILD sha256 + .SRCINFO refreshed"
fi
echo "Wheel / sdist:  dist/"
echo "SHA256:         ${SUM}"
