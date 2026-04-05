# PyInstaller runtime hook: python-mpv uses ctypes.util.find_library("mpv"), which on Linux
# consults ldconfig and loads the host's libmpv. That mixes host FFmpeg (e.g. libavutil.so.60)
# with libs bundled in _MEIPASS (e.g. older libva) and breaks with missing symbols. Force the
# bundled libmpv and prepend _MEIPASS to LD_LIBRARY_PATH for its dependency chain.
import os
import sys


def _apply() -> None:
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return

    old = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = meipass + (os.pathsep + old if old else "")

    import ctypes.util

    _orig = ctypes.util.find_library

    def _find(name: str):
        if name == "mpv":
            for cand in ("libmpv.so.2", "libmpv.so.1", "libmpv.so"):
                p = os.path.join(meipass, cand)
                if os.path.isfile(p):
                    return p
        return _orig(name)

    ctypes.util.find_library = _find  # type: ignore[method-assign]


_apply()
