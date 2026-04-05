#!/usr/bin/env bash
# Assemble PyInstaller tree + linuxdeploy AppImage. Expects cwd = project root.
# Dependencies must already be installed (use appimage-in-docker.sh in Docker).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"

dest="${APPIMAGE_OUTPUT_DIR:-${1:-$root/dist}}"
mkdir -p "$dest"

version=$(grep -m1 '^version = ' "$root/pyproject.toml" | sed -E 's/^version = "([^"]+)".*/\1/' || echo "0.1.0")

rm -rf build dist AppDir
pyinstaller -y --clean hikvision-viewer.spec

# Ubuntu/Debian libmpv often carries RUNPATH to /usr/lib; force deps next to the .so in _internal.
_internal="dist/hikvision-viewer/_internal"
if command -v patchelf >/dev/null 2>&1 && [[ -d "$_internal" ]]; then
	while IFS= read -r -d '' f; do
		file -b "$f" 2>/dev/null | grep -q ELF || continue
		patchelf --set-rpath '$ORIGIN' "$f" 2>/dev/null || true
	done < <(find "$_internal" -type f \( -name '*.so' -o -name '*.so.*' \) ! -path '*/PyQt6/*' -print0 2>/dev/null)
fi

rm -rf AppDir
mkdir -p AppDir/usr/bin
cp -a dist/hikvision-viewer/hikvision-viewer AppDir/usr/bin/
cp -a dist/hikvision-viewer/_internal AppDir/usr/bin/

cp -f "$root/hikvision-viewer.desktop" AppDir/hikvision-viewer.desktop
sed -i 's/^Icon=.*/Icon=hikvision-viewer/' AppDir/hikvision-viewer.desktop
sed -i 's/^Exec=.*/Exec=hikvision-viewer %F/' AppDir/hikvision-viewer.desktop

mkdir -p AppDir/usr/share/icons/hicolor/256x256/apps
icon_png="AppDir/usr/share/icons/hicolor/256x256/apps/hikvision-viewer.png"
if command -v convert >/dev/null 2>&1; then
	convert -size 256x256 xc:'#0f3460' "$icon_png"
else
	echo "ImageMagick (convert) is required to generate the 256x256 AppImage icon." >&2
	exit 1
fi

mkdir -p "$root/tools"
cd "$root/tools"
curl -fsSL -o linuxdeploy-x86_64.AppImage \
	"https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage"
curl -fsSL -o appimagetool-x86_64.AppImage \
	"https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
chmod +x linuxdeploy-x86_64.AppImage appimagetool-x86_64.AppImage

cd "$root"
export APPIMAGE_EXTRACT_AND_RUN=1
export ARCH=x86_64
export VERSION="$version"
ld_tool="$root/tools/linuxdeploy-x86_64.AppImage"
aitool="$root/tools/appimagetool-x86_64.AppImage"
appdir="$root/AppDir"
"$ld_tool" \
	--appdir "$appdir" \
	--executable "$appdir/usr/bin/hikvision-viewer" \
	--desktop-file "$appdir/hikvision-viewer.desktop" \
	--icon-file "$appdir/usr/share/icons/hicolor/256x256/apps/hikvision-viewer.png"

out_name="hikvision-viewer-${version}-x86_64.AppImage"
out_path="$dest/$out_name"
rm -f "$out_path"
"$aitool" "$appdir" "$out_path"
echo "Wrote $out_path"
