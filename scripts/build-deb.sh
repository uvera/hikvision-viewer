#!/usr/bin/env bash
# Build a .deb on Debian/Ubuntu (native) or in Docker when dpkg-buildpackage is missing.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out_dir="${DEB_OUTPUT_DIR:-$root/dist}"
mkdir -p "$out_dir"

run_build() {
	local src="$1"
	local build_root="$2"
	local dest="$3"
	rm -rf "$build_root"
	mkdir -p "$build_root/pkg"
	rsync -a "$src/" "$build_root/pkg/" \
		--exclude .git/ \
		--exclude .venv/ \
		--exclude dist/ \
		--exclude build/ \
		--exclude debian/.debhelper/ \
		--exclude debian/files \
		--exclude 'debian/*.substvars' \
		--exclude 'debian/*.debhelper.log' \
		--exclude debian/hikvision-viewer/ \
		--exclude aur/pkg/ \
		--exclude aur/src/
	(
		cd "$build_root/pkg"
		dpkg-buildpackage -us -uc -b --no-sign
	)
	shopt -s nullglob
	for f in "$build_root"/*.deb "$build_root"/*.changes "$build_root"/*.buildinfo; do
		mv -f "$f" "$dest/"
	done
	shopt -u nullglob
	rm -rf "$build_root"
}

if command -v dpkg-buildpackage >/dev/null 2>&1; then
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' EXIT
	run_build "$root" "$tmp/build" "$out_dir"
	echo "Artifacts under: $out_dir"
	exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
	echo "Install docker.io or debhelper (dpkg-buildpackage) to build the Debian package." >&2
	exit 1
fi

image="${DEB_BUILD_IMAGE:-debian:bookworm-slim}"
docker run --rm \
	-v "$root:/src:ro" \
	-v "$out_dir:/out" \
	"$image" \
	bash -c 'set -euo pipefail
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
	build-essential debhelper dh-python dh-sequence-python3 \
	pybuild-plugin-pyproject python3-all python3-setuptools python3-wheel \
	rsync
run_build() {
	local src="$1" build_root="$2" dest="$3"
	rm -rf "$build_root"
	mkdir -p "$build_root/pkg"
	rsync -a "$src/" "$build_root/pkg/" \
		--exclude .git/ --exclude .venv/ --exclude dist/ --exclude build/ \
		--exclude debian/.debhelper/ --exclude debian/files \
		--exclude debian/hikvision-viewer/ \
		--exclude aur/pkg/ --exclude aur/src/
	(cd "$build_root/pkg" && dpkg-buildpackage -us -uc -b --no-sign)
	shopt -s nullglob
	for f in "$build_root"/*.deb "$build_root"/*.changes "$build_root"/*.buildinfo; do
		mv -f "$f" "$dest/"
	done
	shopt -u nullglob
	rm -rf "$build_root"
}
run_build /src /tmp/hvv-deb /out'
echo "Artifacts under: $out_dir"
