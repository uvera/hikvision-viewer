# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for AppImage bundling (run from project root).
import glob
import os
import re
import subprocess

block_cipher = None

_spec_dir = os.path.dirname(os.path.abspath(SPEC))

# System libs we never ship; mpv uses the host libc stack.
_SKIP_LIB_RE = re.compile(
    r"(?:^|/)ld-linux-x86-64\.so\.|linux-vdso\.so"
    r"|/libc\.so\.6$|/libm\.so\.6$|/libpthread\.so\.0$|/libdl\.so\.2$|/librt\.so\.1$"
)


def _should_skip_bundle(path: str) -> bool:
    if _SKIP_LIB_RE.search(path):
        return True
    pl = path.lower()
    if "nvidia" in pl or "cuda" in pl:
        return True
    return False


def _ldd_needed_paths(so_path: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["ldd", so_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    out = []
    for line in proc.stdout.splitlines():
        if "=>" not in line:
            continue
        part = line.split("=>", 1)[1].strip().split()[0]
        if part.startswith("/") and os.path.isfile(part):
            out.append(os.path.realpath(part))
    return out


def _find_libmpv() -> str | None:
    for base in (
        "/usr/lib/x86_64-linux-gnu",
        "/usr/lib64",
        "/usr/lib",
    ):
        for p in sorted(glob.glob(os.path.join(base, "libmpv.so.*"))):
            rp = os.path.realpath(p)
            if os.path.isfile(rp):
                return rp
    return None


def _libmpv_closure_binaries():
    mpv = _find_libmpv()
    if mpv is None:
        return []

    stack = [mpv]
    seen_ldd: set[str] = set()
    order: list[str] = []
    bundled: set[str] = set()

    while stack:
        cur = stack.pop()
        if cur in seen_ldd:
            continue
        seen_ldd.add(cur)
        if not os.path.isfile(cur):
            continue
        if not _should_skip_bundle(cur):
            if cur not in bundled:
                bundled.add(cur)
                order.append(cur)
        for dep in _ldd_needed_paths(cur):
            if _should_skip_bundle(dep):
                continue
            stack.append(dep)

    return [(p, ".") for p in order]


def _drop_wheel_gcc_runtime(binaries):
    """Avoid shipping libstdc++/libgcc from PyQt wheels while the host loads
    system FFmpeg/rubberband (newer GLIBCXX). Closure below adds one consistent
    copy from the build image for the bundled mpv stack."""

    def keep(t):
        blob = " ".join(str(x) for x in t)
        if "libstdc++.so" in blob:
            return False
        if "libgcc_s.so" in blob:
            return False
        return True

    return [t for t in binaries if keep(t)]


a = Analysis(
    ["hikvision_viewer/__main__.py"],
    pathex=[],
    binaries=_libmpv_closure_binaries(),
    datas=[],
    hiddenimports=[
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "mpv",
        "yaml",
        "cryptography",
        "keyring",
        "dotenv",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(_spec_dir, "pyi_rth_mpv.py")],
    excludes=[],
    noarchive=False,
)

a.binaries = _drop_wheel_gcc_runtime(a.binaries)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="hikvision-viewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="hikvision-viewer",
)
