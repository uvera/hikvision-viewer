#!/usr/bin/env python3
import json
import locale
import logging
import os
import shutil
import socket
import sys
import tempfile
from collections import deque
from pathlib import Path

import mpv
from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPainter, QPalette, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from hikvision_viewer.config_editor import open_config_editor
from hikvision_viewer.config_loader import (
    app_config_dir,
    apply_viewer_from_yaml,
    load_streams,
    ordered_stream_names,
    resolve_config_path,
)
from hikvision_viewer.logging_utils import configure_logging

LOG = logging.getLogger(__name__)
MPV_LOG = logging.getLogger("hikvision_viewer.mpv")

_SIDEBAR_THUMB_W = 120
_SIDEBAR_THUMB_H = 100
_THUMB_REFRESH_MS = 5000


def _sidebar_placeholder_pixmap(title: str, width: int) -> QPixmap:
    pm = QPixmap(max(width, 1), _SIDEBAR_THUMB_H)
    pm.fill(QColor(40, 40, 40))
    p = QPainter(pm)
    p.setPen(QColor(140, 140, 140))
    font = QFont()
    font.setPixelSize(11)
    p.setFont(font)
    label = (title[:10] + "…") if len(title) > 10 else title
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, label or "?")
    p.end()
    return pm


def _fit_sidebar_thumb_pixmap(pm: QPixmap, tw: int, th: int) -> QPixmap:
    """Scale and center-crop so the result fills a tw×th rectangle (full-width previews)."""
    if pm.isNull() or tw < 1 or th < 1:
        return pm
    scaled = pm.scaled(
        tw,
        th,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    sw, sh = scaled.width(), scaled.height()
    x = max(0, (sw - tw) // 2)
    y = max(0, (sh - th) // 2)
    return scaled.copy(QRect(x, y, tw, th))


def _viewer_state_path():
    return app_config_dir() / "viewer_state.json"


def _load_viewer_state() -> dict:
    path = _viewer_state_path()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_viewer_state_file(data: dict) -> None:
    path = _viewer_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _mpv_hwdec() -> str:
    # Multiple embedded players + hwdec often segfaults GPU drivers; opt in with HIKVISION_MPV_HWDEC=auto
    return os.environ.get("HIKVISION_MPV_HWDEC", "no").strip() or "no"


def _mpv_vo() -> str:
    return os.environ.get("HIKVISION_MPV_VO", "gpu").strip() or "gpu"


def _mpv_gpu_context() -> str:
    """Optional explicit mpv GPU context; empty lets mpv auto-pick."""
    return os.environ.get("HIKVISION_MPV_GPU_CONTEXT", "").strip()


def _subprocess_gpu_context_for_embed(vo: str) -> str:
    """Use an X11-compatible context for --wid embedding unless explicitly overridden."""
    explicit = _mpv_gpu_context()
    if explicit:
        return explicit
    if sys.platform.startswith("linux") and vo in ("gpu", "gpu-next"):
        # Wayland GPU contexts can ignore --wid and spawn separate windows.
        # x11egl is generally more robust for embedded --wid rendering than plain x11.
        return "x11egl"
    return ""


def _use_mpv_subprocess() -> bool:
    # PyInstaller/AppImage ships libmpv, not the `mpv` binary; subprocess mode needs mpv on PATH.
    default = "0" if getattr(sys, "frozen", False) else "1"
    return _env_flag("HIKVISION_MPV_SUBPROCESS", default)


def _force_dark_mode() -> bool:
    return _env_flag("HIKVISION_FORCE_DARK", "0")


def _mpv_debug_enabled() -> bool:
    return _env_flag("HIKVISION_DEBUG_MPV", "0")


def _log_mpv(msg: str) -> None:
    if _mpv_debug_enabled():
        MPV_LOG.debug("%s", msg)


def _apply_fusion_dark_palette(app: QApplication) -> None:
    """Dark Fusion palette (ignores system light theme for Qt widgets)."""
    palette = QPalette()
    c_window = QColor(53, 53, 53)
    c_window_text = QColor(220, 220, 220)
    c_base = QColor(35, 35, 35)
    c_alt = QColor(45, 45, 45)
    c_highlight = QColor(64, 128, 200)
    c_disabled = QColor(127, 127, 127)

    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    ):
        palette.setColor(group, QPalette.ColorRole.Window, c_window)
        palette.setColor(group, QPalette.ColorRole.WindowText, c_window_text)
        palette.setColor(group, QPalette.ColorRole.Base, c_base)
        palette.setColor(group, QPalette.ColorRole.AlternateBase, c_alt)
        palette.setColor(group, QPalette.ColorRole.ToolTipBase, c_base)
        palette.setColor(group, QPalette.ColorRole.ToolTipText, c_window_text)
        palette.setColor(group, QPalette.ColorRole.Text, c_window_text)
        palette.setColor(group, QPalette.ColorRole.Button, c_window)
        palette.setColor(group, QPalette.ColorRole.ButtonText, c_window_text)
        palette.setColor(group, QPalette.ColorRole.Link, QColor(100, 180, 255))
        palette.setColor(group, QPalette.ColorRole.Highlight, c_highlight)
        palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
        palette.setColor(group, QPalette.ColorRole.PlaceholderText, c_disabled)

    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, c_disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, c_disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, c_disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor(80, 80, 80))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, c_disabled
    )

    app.setPalette(palette)


def _apply_qt_platform_for_wid_embed() -> None:
    """mpv --wid (and libmpv wid) need a real X11 window id; Qt on Wayland does not provide one."""
    if not sys.platform.startswith("linux"):
        return
    if _env_flag("HIKVISION_QT_WAYLAND", "0"):
        return
    qpa = (os.environ.get("QT_QPA_PLATFORM") or "").strip().lower()
    if qpa and qpa not in ("wayland", ""):
        return
    # Some launchers sanitize WAYLAND_DISPLAY and skip the older Wayland check,
    # but --wid embedding still requires an X11-capable Qt backend.
    os.environ["QT_QPA_PLATFORM"] = "xcb"


def _strip_wayland_so_mpv_uses_x11() -> None:
    """If Qt runs on XWayland (xcb), mpv must not see Wayland or it ignores --wid and opens its own windows."""
    if not sys.platform.startswith("linux"):
        return
    if _env_flag("HIKVISION_QT_WAYLAND", "0"):
        return
    if (os.environ.get("QT_QPA_PLATFORM") or "").strip().lower() != "xcb":
        return
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ.pop("WAYLAND_SOCKET", None)


def _log_display_env() -> None:
    if not sys.platform.startswith("linux"):
        return
    LOG.info(
        "Display env: XDG_SESSION_TYPE=%r QT_QPA_PLATFORM=%r DISPLAY=%r WAYLAND_DISPLAY=%r HIKVISION_QT_WAYLAND=%r",
        os.environ.get("XDG_SESSION_TYPE"),
        os.environ.get("QT_QPA_PLATFORM"),
        os.environ.get("DISPLAY"),
        os.environ.get("WAYLAND_DISPLAY"),
        os.environ.get("HIKVISION_QT_WAYLAND"),
    )


def _x11_embed_unavailable_reason() -> str | None:
    """Return a user-facing reason when --wid embedding cannot work in current launcher env."""
    if not sys.platform.startswith("linux"):
        return None
    qpa = (os.environ.get("QT_QPA_PLATFORM") or "").strip().lower()
    if qpa != "xcb":
        shown = qpa or "<auto>"
        return (
            f"Qt platform is {shown!r}; mpv --wid embedding needs X11/XWayland "
            "(set QT_QPA_PLATFORM=xcb)"
        )
    if not os.environ.get("DISPLAY"):
        return "DISPLAY is not set; X11/XWayland is unavailable in this launcher environment"
    return None


def _mpv_subprocess_environment() -> QProcessEnvironment:
    env = QProcessEnvironment.systemEnvironment()
    if sys.platform.startswith("linux") and not _env_flag("HIKVISION_QT_WAYLAND", "0"):
        if (os.environ.get("QT_QPA_PLATFORM") or "").strip().lower() == "xcb":
            env.remove("WAYLAND_DISPLAY")
            env.remove("WAYLAND_SOCKET")
    return env


_LAVF_RECONNECT = "reconnect_streamed=1,reconnect_delay_max=5"


def _mpv_ipc_payload(name: str, value: object) -> bytes:
    # Minified JSON; mpv requires a single line terminated by \n (see DOCS/man/ipc.rst).
    line = json.dumps(
        {"command": ["set_property", name, value]}, separators=(",", ":")
    ) + "\n"
    return line.encode("utf-8")


def _mpv_ipc_line_looks_like_command_reply(text: str) -> bool:
    """mpv may emit {"event":...} lines before {"error":"success",...} command replies."""
    t = text.strip()
    if not t or t.startswith("#"):
        return False
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return False
    return "error" in obj


def _mpv_ipc_read_command_reply_unix(client: socket.socket) -> str:
    """Read lines until a JSON command reply (has 'error'); skip event/property-change lines."""
    buf = b""
    chunk: bytes = b""
    try:
        while True:
            while b"\n" not in buf:
                chunk = client.recv(8192)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 262144:
                    break
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                text = line.decode("utf-8", errors="replace").strip()
                if _mpv_ipc_line_looks_like_command_reply(text):
                    return text[:800]
            if not chunk:
                break
    except OSError as e:
        return f"<read error: {e}>"
    return ""


def _mpv_ipc_read_command_reply_pipe(pipe, buf: bytearray) -> str:
    while True:
        while b"\n" not in buf:
            chunk = pipe.read(8192)
            if not chunk:
                return ""
            buf.extend(chunk)
            if len(buf) > 262144:
                return ""
        while b"\n" in buf:
            idx = buf.find(b"\n")
            line = bytes(buf[:idx])
            del buf[: idx + 1]
            text = line.decode("utf-8", errors="replace").strip()
            if _mpv_ipc_line_looks_like_command_reply(text):
                return text[:800]


def _mpv_ipc_send_unix(socket_path: str, data: bytes) -> tuple[bool, str]:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.35)
        client.connect(socket_path)
        client.sendall(data)
        reply = _mpv_ipc_read_command_reply_unix(client)
        client.close()
        return True, reply
    except OSError as e:
        return False, str(e)


def _mpv_ipc_send_win32(pipe_path: str, data: bytes) -> tuple[bool, str]:
    """mpv on Windows uses a named pipe (see --input-ipc-server=\\\\.\\pipe\\...)."""
    try:
        with open(pipe_path, "r+b", buffering=0) as pipe:
            pipe.write(data)
            buf = bytearray()
            reply = _mpv_ipc_read_command_reply_pipe(pipe, buf)
            return True, reply
    except OSError as e:
        return False, str(e)


def _mpv_ipc_set_property(
    ipc_path: str, name: str, value: object, *, stream: str = ""
) -> None:
    """Best-effort mpv JSON IPC: Unix domain socket (Linux/macOS) or named pipe (Windows)."""
    if not ipc_path:
        return
    data = _mpv_ipc_payload(name, value)
    label = f"{stream!r} " if stream else ""
    _log_mpv(f"{label}ipc send {name}={value!r} ({len(data)} B) path={ipc_path!r}")
    if sys.platform == "win32":
        ok, detail = _mpv_ipc_send_win32(ipc_path, data)
    else:
        ok, detail = _mpv_ipc_send_unix(ipc_path, data)
    if ok:
        _log_mpv(f"{label}ipc recv {detail!r}")
    else:
        _log_mpv(f"{label}ipc FAILED: {detail!r}")


def _mpv_ipc_set_both_mutes(ipc_path: str, stream: str, muted: bool) -> None:
    """mute + ao-mute: RTSP/audio sometimes ignores mute alone (mpv issue #10328 area)."""
    _mpv_ipc_set_property(ipc_path, "mute", muted, stream=stream)
    _mpv_ipc_set_property(ipc_path, "ao-mute", muted, stream=stream)


def _mpv_ipc_reply_ok(line: str) -> bool:
    if not line or line.startswith(("<drain error", "<read error")):
        return False
    try:
        return json.loads(line).get("error") == "success"
    except json.JSONDecodeError:
        return False


def _mpv_parse_mute_reply_line(line: str) -> bool | None:
    if not line or line.startswith(("<drain error", "<read error")):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if obj.get("error") != "success":
        return None
    val = obj.get("data")
    if isinstance(val, bool):
        return val
    return None


def _mpv_ipc_get_mute_ao_pair(ipc_path: str, stream: str) -> tuple[bool | None, bool | None]:
    """Read mute and ao-mute in one IPC session (one connection, two get_property round-trips)."""
    if not ipc_path:
        return None, None
    get_m = (
        json.dumps({"command": ["get_property", "mute"]}, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    get_ao = (
        json.dumps({"command": ["get_property", "ao-mute"]}, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    label = f"{stream!r} " if stream else ""
    try:
        if sys.platform == "win32":
            pbuf = bytearray()
            with open(ipc_path, "r+b", buffering=0) as pipe:
                pipe.write(get_m)
                r1 = _mpv_ipc_read_command_reply_pipe(pipe, pbuf)
                m = _mpv_parse_mute_reply_line(r1)
                pipe.write(get_ao)
                r2 = _mpv_ipc_read_command_reply_pipe(pipe, pbuf)
                ao = _mpv_parse_mute_reply_line(r2)
            return m, ao
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.35)
        client.connect(ipc_path)
        try:
            client.sendall(get_m)
            r1 = _mpv_ipc_read_command_reply_unix(client)
            m = _mpv_parse_mute_reply_line(r1)
            client.sendall(get_ao)
            r2 = _mpv_ipc_read_command_reply_unix(client)
            ao = _mpv_parse_mute_reply_line(r2)
        finally:
            client.close()
        return m, ao
    except OSError as e:
        _log_mpv(f"{label}ipc get mute/ao-mute pair FAILED: {e!r}")
        return None, None


def _mpv_ipc_atomic_snapshot_mute_and_set_mute(ipc_path: str, stream: str) -> bool | None:
    """One IPC session: read mute (for restore on show), then set mute true. Avoids lost set on some mpv builds."""
    if not ipc_path:
        return None
    get_b = (
        json.dumps({"command": ["get_property", "mute"]}, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    set_mute_b = _mpv_ipc_payload("mute", True)
    set_ao_b = _mpv_ipc_payload("ao-mute", True)
    label = f"{stream!r} " if stream else ""
    snap: bool | None = None
    try:
        if sys.platform == "win32":
            _log_mpv(
                f"{label}ipc atomic hide: get mute + mute/ao-mute=true pipe={ipc_path!r}"
            )
            pbuf = bytearray()
            with open(ipc_path, "r+b", buffering=0) as pipe:
                pipe.write(get_b)
                r1 = _mpv_ipc_read_command_reply_pipe(pipe, pbuf)
                _log_mpv(f"{label}ipc atomic recv1 {r1!r}")
                snap = _mpv_parse_mute_reply_line(r1)
                pipe.write(set_mute_b)
                r2 = _mpv_ipc_read_command_reply_pipe(pipe, pbuf)
                _log_mpv(f"{label}ipc atomic recv2 {r2!r}")
                if not _mpv_ipc_reply_ok(r2):
                    pipe.write(set_mute_b)
                    r2b = _mpv_ipc_read_command_reply_pipe(pipe, pbuf)
                    _log_mpv(f"{label}ipc atomic recv2b retry {r2b!r}")
                pipe.write(set_ao_b)
                r3 = _mpv_ipc_read_command_reply_pipe(pipe, pbuf)
                _log_mpv(f"{label}ipc atomic recv3 ao-mute {r3!r}")
        else:
            _log_mpv(
                f"{label}ipc atomic hide: get mute + mute/ao-mute=true sock={ipc_path!r}"
            )
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(0.45)
            client.connect(ipc_path)
            try:
                client.sendall(get_b)
                r1 = _mpv_ipc_read_command_reply_unix(client)
                _log_mpv(f"{label}ipc atomic recv1 {r1!r}")
                snap = _mpv_parse_mute_reply_line(r1)
                client.sendall(set_mute_b)
                r2 = _mpv_ipc_read_command_reply_unix(client)
                _log_mpv(f"{label}ipc atomic recv2 {r2!r}")
                if not _mpv_ipc_reply_ok(r2):
                    client.sendall(set_mute_b)
                    r2b = _mpv_ipc_read_command_reply_unix(client)
                    _log_mpv(f"{label}ipc atomic recv2b retry {r2b!r}")
                client.sendall(set_ao_b)
                r3 = _mpv_ipc_read_command_reply_unix(client)
                _log_mpv(f"{label}ipc atomic recv3 ao-mute {r3!r}")
            finally:
                client.close()
    except OSError as e:
        _log_mpv(f"{label}ipc atomic hide FAILED: {e!r}")
    return snap


class _LibmpvMuteBridge(QObject):
    """mpv invokes key callbacks on its event thread; emit here so Qt delivers on the GUI thread."""

    toggle_mute = pyqtSignal()


class StreamTile(QWidget):
    """One camera: label + native surface. Drives mpv either via QProcess (default) or embedded libmpv."""

    def __init__(self, title: str, url: str, subprocess: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._url = url
        self._subprocess = subprocess
        self._player: mpv.MPV | None = None
        self._proc: QProcess | None = None
        self._proc_output_tail: list[str] = []
        self._ipc_path: str | None = None
        self._shutting_down = False
        self._start_wait_attempts = 0
        self._embed_restart_attempts = 0
        self._started = False
        self._audio_muted_by_user = True
        self._mute_suppressed_single_stack = False
        # mpv mute before we IPC-mute for stack hide; on show, unmute only if this was False (was audible).
        self._mute_snapshot_before_stack_hide: bool | None = None
        self._mute_needs_stack_hide_ipc_roundtrip = False
        self._subprocess_mute_sync_timer: QTimer | None = None
        self._libmpv_mute_bridge: _LibmpvMuteBridge | None = None
        self._libmpv_m_key_binding: object | None = None

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self._label = QLabel(title)
        self._label.setStyleSheet("color: #ccc; font-size: 12px;")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        header.addWidget(self._label, stretch=1)
        self._mute_btn: QToolButton | None = None
        if not subprocess:
            self._mute_btn = QToolButton()
            self._mute_btn.setAutoRaise(True)
            self._mute_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._mute_btn.setIconSize(QSize(20, 20))
            self._mute_btn.setEnabled(False)
            self._mute_btn.clicked.connect(self._toggle_libmpv_mute)
            self._sync_mute_button()
            header.addWidget(self._mute_btn, alignment=Qt.AlignmentFlag.AlignRight)
            self._libmpv_mute_bridge = _LibmpvMuteBridge(self)
            self._libmpv_mute_bridge.toggle_mute.connect(self._toggle_libmpv_mute)
            sc_m = QShortcut(QKeySequence(Qt.Key.Key_M), self)
            sc_m.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc_m.activated.connect(self._toggle_libmpv_mute)
        layout.addLayout(header)

        self._surface = QWidget()
        self._surface.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self._surface.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self._surface.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._surface.setMinimumSize(280, 158)
        self._surface.setStyleSheet("background: #000;")
        layout.addWidget(self._surface, stretch=1)

    @property
    def stream_name(self) -> str:
        return self._title

    @property
    def stream_url(self) -> str:
        return self._url

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._started:
            return
        self._started = True
        self._start_wait_attempts = 0
        self._embed_restart_attempts = 0
        # Map the X11 window before mpv attaches; subprocess needs a real mapped wid.
        delay_ms = 150 if self._subprocess else 0
        QTimer.singleShot(delay_ms, self._start_player)

    def _schedule_start_retry(self, reason: str, delay_ms: int = 120) -> bool:
        self._start_wait_attempts += 1
        if self._start_wait_attempts > 25:
            LOG.error(
                "Timed out waiting for stable native window for %s (%s)",
                self._title,
                reason,
            )
            self._label.setText(f"{self._title} (surface init timeout)")
            return False
        LOG.debug(
            "Delaying stream start for %s: %s (attempt %d/25)",
            self._title,
            reason,
            self._start_wait_attempts,
        )
        QTimer.singleShot(delay_ms, self._start_player)
        return True

    def _start_player(self) -> None:
        if self._proc is not None or self._player is not None:
            return
        embed_reason = _x11_embed_unavailable_reason()
        if embed_reason:
            self._label.setText(f"{self._title} (X11/XWayland unavailable)")
            LOG.error("Cannot start embedded mpv for %s: %s", self._title, embed_reason)
            return
        if not self._surface.isVisible():
            self._schedule_start_retry("surface not visible")
            return
        if not self.isVisible():
            self._schedule_start_retry("tile not visible")
            return
        handle = self._surface.windowHandle()
        if handle is not None and not handle.isExposed():
            self._schedule_start_retry("surface window handle not exposed yet")
            return
        try:
            wid = int(self._surface.winId())
        except Exception:
            LOG.exception("Could not get window id for tile: %s", self._title)
            self._label.setText(f"{self._title} (no window id)")
            return
        if wid <= 0:
            self._schedule_start_retry(f"invalid window id {wid}")
            return
        LOG.info("Starting stream tile: %s (subprocess=%s)", self._title, self._subprocess)
        if self._subprocess:
            self._start_subprocess(wid)
        else:
            self._start_libmpv(wid)

    def _start_subprocess(self, wid: int) -> None:
        self._shutting_down = False
        exe = shutil.which("mpv")
        if not exe:
            LOG.error("mpv binary not found in PATH for stream %s", self._title)
            self._label.setText(f"{self._title} (mpv not in PATH)")
            return
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.setProcessEnvironment(_mpv_subprocess_environment())
        hwdec, vo = _mpv_hwdec(), _mpv_vo()
        if sys.platform == "win32":
            ipc = rf"\\.\pipe\hikvision-viewer-mpv-{os.getpid()}-{id(self)}"
        else:
            ipc = os.path.join(
                tempfile.gettempdir(),
                f"hikvision-viewer-mpv-{os.getpid()}-{id(self)}.sock",
            )
            try:
                os.unlink(ipc)
            except OSError:
                pass
        self._ipc_path = ipc
        ipc_args = [f"--input-ipc-server={ipc}"]
        args = [
            "--no-terminal",
            "--mute",
            *ipc_args,
            "--keep-open=yes",
            f"--wid={wid}",
            "--rtsp-transport=tcp",
            "--cache=no",
            "--demuxer-max-bytes=32MiB",
            f"--hwdec={hwdec}",
            f"--vo={vo}",
            "--video-latency-hacks=yes",
            f"--stream-lavf-o={_LAVF_RECONNECT}",
            "--msg-level=all=no",
        ]
        gpu_context = _subprocess_gpu_context_for_embed(vo)
        if gpu_context:
            args.append(f"--gpu-context={gpu_context}")
        args.append(self._url)
        proc.finished.connect(self._on_proc_finished)
        proc.readyReadStandardOutput.connect(self._on_proc_output_ready)
        # Do not use errorOccurred(ProcessError): PyQt6 may fail converting the enum to Python
        # (TypeError: unable to convert C++ 'QProcess::ProcessError'...). Slot takes no args; read
        # error from the process (same as Qt allows for slots with fewer parameters than the signal).
        proc.errorOccurred.connect(self._on_proc_error)
        proc.started.connect(self._on_subprocess_started)
        self._proc = proc
        LOG.info("Launching mpv subprocess for stream %s", self._title)
        proc.start(exe, args)

    def _on_proc_output_ready(self) -> None:
        proc = self._proc
        if proc is None:
            return
        out = bytes(proc.readAllStandardOutput())
        if not out:
            return
        text = out.decode("utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            self._proc_output_tail.append(line)
            if len(self._proc_output_tail) > 60:
                self._proc_output_tail = self._proc_output_tail[-60:]
            LOG.debug("mpv[%s]: %s", self._title, line)

    def _on_proc_error(self) -> None:
        if self._shutting_down:
            return
        proc = self._proc
        if proc is None:
            return
        try:
            err = proc.error()
        except Exception:
            self._label.setText(f"{self._title} (mpv start error)")
            return
        # Compare to enum members without int() — int(ProcessError) can also throw on some PyQt6 builds.
        if err == QProcess.ProcessError.FailedToStart:
            detail = "failed to start"
        elif err == QProcess.ProcessError.Crashed:
            detail = "crashed"
        elif err == QProcess.ProcessError.Timedout:
            detail = "timed out"
        elif err == QProcess.ProcessError.ReadError:
            detail = "read error"
        elif err == QProcess.ProcessError.WriteError:
            detail = "write error"
        elif err == QProcess.ProcessError.UnknownError:
            detail = "unknown error"
        else:
            detail = "error"
        self._label.setText(f"{self._title} (mpv start error: {detail})")
        LOG.error("mpv subprocess error for %s: %s", self._title, detail)

    def _on_proc_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        if self._proc is None:
            return
        if self._shutting_down:
            if status == QProcess.ExitStatus.CrashExit or code != 0:
                LOG.debug(
                    "mpv subprocess exited during shutdown for %s: code=%s status=%s",
                    self._title,
                    code,
                    status,
                )
            return
        tail = "\n".join(self._proc_output_tail[-12:])
        bad_window = "BadWindow" in tail or "X_DestroyWindow" in tail
        if (
            code != 0
            and bad_window
            and self._subprocess
            and self._embed_restart_attempts < 2
        ):
            self._embed_restart_attempts += 1
            LOG.info(
                "mpv embed startup race for %s (BadWindow), retrying (%d/2)",
                self._title,
                self._embed_restart_attempts,
            )
            self._proc.deleteLater()
            self._proc = None
            self._proc_output_tail.clear()
            QTimer.singleShot(220, self._start_player)
            return
        if status == QProcess.ExitStatus.CrashExit:
            self._label.setText(f"{self._title} (mpv crashed)")
            if tail:
                LOG.error("mpv subprocess crashed for %s. tail:\n%s", self._title, tail)
            else:
                LOG.error("mpv subprocess crashed for stream %s", self._title)
        elif code != 0:
            self._label.setText(f"{self._title} (mpv exited {code})")
            if tail:
                LOG.warning(
                    "mpv subprocess exited non-zero for %s: %s. tail:\n%s",
                    self._title,
                    code,
                    tail,
                )
            else:
                LOG.warning("mpv subprocess exited non-zero for %s: %s", self._title, code)

    def _on_subprocess_started(self) -> None:
        self._embed_restart_attempts = 0
        QTimer.singleShot(100, self._subprocess_startup_audio_sync)
        if self._subprocess_mute_sync_timer is None:
            t = QTimer(self)
            t.setInterval(350)
            t.timeout.connect(self._subprocess_sync_mute_ao_if_diverged)
            self._subprocess_mute_sync_timer = t
        QTimer.singleShot(200, self._subprocess_start_mute_poll_timer)

    def _subprocess_startup_audio_sync(self) -> None:
        """CLI --mute can leave ao-mute out of sync until IPC sets both; fixes first unmute with no audio."""
        if self._ipc_path is None or self._proc is None:
            return
        if self._proc.state() != QProcess.ProcessState.Running:
            return
        if self._mute_suppressed_single_stack:
            return
        if self._audio_muted_by_user:
            _mpv_ipc_set_both_mutes(self._ipc_path, self._title, True)
        self._apply_output_mute()

    def _schedule_subprocess_unmute_reassert(self) -> None:
        ipc, title = self._ipc_path, self._title
        proc = self._proc

        def retry() -> None:
            if (
                self._ipc_path != ipc
                or self._proc is not proc
                or proc is None
                or proc.state() != QProcess.ProcessState.Running
            ):
                return
            if self._mute_suppressed_single_stack or self._audio_muted_by_user:
                return
            _mpv_ipc_set_both_mutes(ipc, title, False)

        QTimer.singleShot(150, retry)

    def _subprocess_start_mute_poll_timer(self) -> None:
        if self._subprocess_mute_sync_timer is None or self._proc is None:
            return
        if self._proc.state() != QProcess.ProcessState.Running:
            return
        self._subprocess_mute_sync_timer.start()

    def _subprocess_sync_mute_ao_if_diverged(self) -> None:
        """mpv key 'm' can flip mute but leave ao-mute stale; we almost never call _apply_output_mute for subprocess."""
        if not self._subprocess or self._ipc_path is None or self._proc is None:
            return
        if self._proc.state() != QProcess.ProcessState.Running:
            return
        if self._mute_suppressed_single_stack:
            return
        m, ao = _mpv_ipc_get_mute_ao_pair(self._ipc_path, self._title)
        if m is None or ao is None or m == ao:
            return
        if _mpv_debug_enabled():
            _log_mpv(
                f"{self._title!r} ipc mute/ao-mute diverged mute={m!r} ao-mute={ao!r} -> set both to {m!r}"
            )
        _mpv_ipc_set_both_mutes(self._ipc_path, self._title, m)

    def _effective_audio_mute(self) -> bool:
        return self._mute_suppressed_single_stack or self._audio_muted_by_user

    def _apply_output_mute(self) -> None:
        want = self._effective_audio_mute()
        if self._player is not None:
            try:
                self._player.mute = want
                self._player.ao_mute = want
            except Exception:
                pass
            if _mpv_debug_enabled():
                _log_mpv(
                    f"{self._title!r} libmpv mute={want} "
                    f"(user={self._audio_muted_by_user} stack_hide={self._mute_suppressed_single_stack})"
                )
        elif self._ipc_path and self._proc is not None:
            if self._proc.state() != QProcess.ProcessState.Running:
                if _mpv_debug_enabled():
                    _log_mpv(f"{self._title!r} ipc skip: process not running")
                return
            # Subprocess: do not send mute=true on every visible refresh (overwrites mpv "m").
            # Stack hide: IPC mute true. Stack show: IPC mute false only if mute was false before hide.
            if self._mute_suppressed_single_stack:
                if self._mute_needs_stack_hide_ipc_roundtrip:
                    self._mute_needs_stack_hide_ipc_roundtrip = False
                    self._mute_snapshot_before_stack_hide = (
                        _mpv_ipc_atomic_snapshot_mute_and_set_mute(
                            self._ipc_path, self._title
                        )
                    )
                    if _mpv_debug_enabled():
                        _log_mpv(
                            f"{self._title!r} stack hide atomic done "
                            f"snapshot={self._mute_snapshot_before_stack_hide!r}"
                        )
                # Second IPC: reinforce mute + ao-mute (hidden tiles may not get another apply).
                _mpv_ipc_set_both_mutes(self._ipc_path, self._title, True)
            elif self._mute_snapshot_before_stack_hide is not None:
                snap = self._mute_snapshot_before_stack_hide
                self._mute_snapshot_before_stack_hide = None
                if snap is False:
                    _mpv_ipc_set_both_mutes(self._ipc_path, self._title, False)
                    self._schedule_subprocess_unmute_reassert()
                elif _mpv_debug_enabled():
                    _log_mpv(
                        f"{self._title!r} stack show: keep mute (was muted before hide, snap=True)"
                    )
            elif not self._audio_muted_by_user:
                _mpv_ipc_set_both_mutes(self._ipc_path, self._title, False)
                self._schedule_subprocess_unmute_reassert()
            elif _mpv_debug_enabled():
                _log_mpv(
                    f"{self._title!r} ipc skip mute=true while visible "
                    f"(preserve mpv / user key); user_pref_muted={self._audio_muted_by_user}"
                )

    def set_single_stack_mute_suppressed(self, suppressed: bool) -> None:
        """In single/stacked mode, hidden tiles must stay muted so only the visible page outputs audio."""
        prev = self._mute_suppressed_single_stack
        if self._subprocess and suppressed and not prev:
            self._mute_needs_stack_hide_ipc_roundtrip = True
        self._mute_suppressed_single_stack = suppressed
        # Always refresh IPC/libmpv when a tile is visible: if we skipped earlier while suppressed was
        # already False, we never sent unmute after mpv was forced muted while hidden.
        if not suppressed or prev != suppressed:
            self._apply_output_mute()

    def _start_libmpv(self, wid: int) -> None:
        vo = _mpv_vo()
        gpu_context = _mpv_gpu_context()
        opts: dict = dict(
            wid=str(wid),
            vo=vo,
            mute=True,
            hwdec=_mpv_hwdec(),
            rtsp_transport="tcp",
            cache="no",
            demuxer_max_bytes="32MiB",
            video_latency_hacks=True,
            stream_lavf_o=_LAVF_RECONNECT,
            loglevel="warn",
            input_default_bindings=True,
            input_vo_keyboard=True,
        )
        if gpu_context:
            opts["gpu_context"] = gpu_context
        LOG.info("Launching libmpv for stream %s", self._title)
        self._player = mpv.MPV(**opts)
        bridge = self._libmpv_mute_bridge
        if bridge is not None:

            @self._player.on_key_press("m")
            def _libmpv_m_key() -> None:
                bridge.toggle_mute.emit()

            self._libmpv_m_key_binding = _libmpv_m_key
        self._player.play(self._url)
        self._apply_output_mute()
        QTimer.singleShot(200, self._apply_output_mute)
        if self._mute_btn is not None:
            self._mute_btn.setEnabled(True)
            self._sync_mute_button()

    def _sync_mute_button(self) -> None:
        if self._mute_btn is None:
            return
        style = self.style()
        muted = self._audio_muted_by_user
        if muted:
            self._mute_btn.setIcon(
                style.standardIcon(QStyle.StandardPixmap.SP_MediaVolumeMuted)
            )
            self._mute_btn.setToolTip("Unmute")
        else:
            self._mute_btn.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaVolume))
            self._mute_btn.setToolTip("Mute")

    def _toggle_libmpv_mute(self) -> None:
        if self._player is None:
            return
        self._audio_muted_by_user = not self._audio_muted_by_user
        self._apply_output_mute()
        self._sync_mute_button()
        if not self._audio_muted_by_user:

            def _kick_audio_output() -> None:
                if self._player is None or self._audio_muted_by_user:
                    return
                try:
                    self._player.command("ao-reload")
                except Exception:
                    pass
                self._apply_output_mute()

            QTimer.singleShot(150, self._apply_output_mute)
            QTimer.singleShot(320, _kick_audio_output)
            QTimer.singleShot(550, self._apply_output_mute)

    def shutdown(self) -> None:
        LOG.debug("Shutting down stream tile: %s", self._title)
        self._shutting_down = True
        if self._subprocess_mute_sync_timer is not None:
            self._subprocess_mute_sync_timer.stop()
        if self._proc is not None:
            if self._proc.state() != QProcess.ProcessState.NotRunning:
                self._proc.terminate()
                if not self._proc.waitForFinished(2500):
                    self._proc.kill()
                    self._proc.waitForFinished(1500)
            self._proc.deleteLater()
            self._proc = None
            self._proc_output_tail.clear()
        if self._ipc_path:
            if sys.platform != "win32":
                try:
                    os.unlink(self._ipc_path)
                except OSError:
                    pass
            self._ipc_path = None
        if self._mute_btn is not None:
            self._mute_btn.setEnabled(False)
        if self._player is not None:
            kb = self._libmpv_m_key_binding
            self._libmpv_m_key_binding = None
            if kb is not None:
                try:
                    kb.unregister_mpv_key_bindings()
                except Exception:
                    pass
            try:
                self._player.terminate()
            except Exception:
                pass
            self._player = None
        self._mute_suppressed_single_stack = False
        self._mute_snapshot_before_stack_hide = None
        self._mute_needs_stack_hide_ipc_roundtrip = False
        self._start_wait_attempts = 0
        self._embed_restart_attempts = 0
        self._started = False


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        LOG.info("Initializing main window")
        self.setWindowTitle("Hikvision RTSP viewer")
        self.resize(1280, 720)
        self._tiles: list[StreamTile] = []
        self._subprocess = _use_mpv_subprocess()
        self._viewer_state = _load_viewer_state()
        self._single_view = bool(self._viewer_state.get("single_view"))
        self._single_index = 0
        self._status_base = ""

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        bar = QHBoxLayout()
        self._status = QLabel()
        self._status.setStyleSheet("color: #888;")
        bar.addWidget(self._status)
        bar.addStretch()

        self._btn_grid = QPushButton("Grid")
        self._btn_grid.setCheckable(True)
        self._btn_single = QPushButton("Single")
        self._btn_single.setCheckable(True)
        self._view_group = QButtonGroup(self)
        self._view_group.addButton(self._btn_grid, 0)
        self._view_group.addButton(self._btn_single, 1)
        self._btn_grid.setChecked(True)
        self._btn_grid.clicked.connect(lambda: self._set_single_view(False))
        self._btn_single.clicked.connect(lambda: self._set_single_view(True))
        bar.addWidget(self._btn_grid)
        bar.addWidget(self._btn_single)

        self._btn_prev = QPushButton("Prev")
        self._btn_prev.setToolTip("Previous camera (Single view); Left/Up")
        self._btn_prev.clicked.connect(self._single_prev)
        bar.addWidget(self._btn_prev)
        self._btn_next = QPushButton("Next")
        self._btn_next.setToolTip("Next camera (Single view); Right/Down")
        self._btn_next.clicked.connect(self._single_next)
        bar.addWidget(self._btn_next)

        fs_btn = QPushButton("Fullscreen")
        fs_btn.setToolTip("Toggle fullscreen (F11)")
        fs_btn.clicked.connect(self._toggle_fullscreen)
        bar.addWidget(fs_btn)

        settings_btn = QToolButton()
        settings_btn.setText("Settings")
        settings_btn.setToolTip("Configuration and environment")
        settings_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        settings_menu = QMenu(settings_btn)
        act_reload = QAction("Reload config", self)
        act_reload.triggered.connect(self._reload)
        settings_menu.addAction(act_reload)
        act_edit = QAction("Edit configuration…", self)
        act_edit.setToolTip(
            "Edit streams, Hikvision URL builder, playback options, and encrypted .env.enc. "
            "Playback (viewer:) changes need an app restart to apply fully."
        )
        act_edit.triggered.connect(self._edit_configuration)
        settings_menu.addAction(act_edit)
        settings_btn.setMenu(settings_menu)
        bar.addWidget(settings_btn)
        outer.addLayout(bar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: #1a1a1a; }")

        self._grid_host = QWidget()
        self._grid_host.setStyleSheet("background: #111;")
        self._grid = QGridLayout(self._grid_host)
        self._grid.setSpacing(8)
        self._scroll.setWidget(self._grid_host)
        outer.addWidget(self._scroll, stretch=1)

        self._single_view_host = QWidget()
        self._single_view_host.setStyleSheet("background: #111;")
        single_outer = QHBoxLayout(self._single_view_host)
        single_outer.setContentsMargins(0, 0, 0, 0)
        single_outer.setSpacing(8)

        self._camera_sidebar = QListWidget()
        self._camera_sidebar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._camera_sidebar.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._camera_sidebar.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._camera_sidebar.setMaximumWidth(260)
        self._camera_sidebar.setMinimumWidth(200)
        self._camera_sidebar.setStyleSheet(
            "QListWidget { background: #1a1a1a; border: none; outline: none; }"
            "QListWidget::item { padding: 6px; border-radius: 4px; }"
            "QListWidget::item:selected { background: #3a5a80; }"
            "QListWidget::item:hover { background: #2a2a2a; }"
        )
        self._camera_sidebar.currentRowChanged.connect(self._on_sidebar_current_row_changed)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: #111;")
        self._stack.currentChanged.connect(self._on_stack_index_changed)

        single_outer.addWidget(self._camera_sidebar)
        single_outer.addWidget(self._stack, stretch=1)

        outer.addWidget(self._single_view_host, stretch=1)
        self._single_view_host.hide()

        self._thumb_generation = 0
        self._thumb_cycle_active = False
        self._thumb_queue: deque[tuple[int, str]] = deque()
        self._thumb_proc: QProcess | None = None
        self._thumb_out_path: Path | None = None
        self._thumb_refresh_timer = QTimer(self)
        self._thumb_refresh_timer.setInterval(_THUMB_REFRESH_MS)
        self._thumb_refresh_timer.timeout.connect(self._start_thumbnail_refresh_cycle)

        self._setup_shortcuts()
        self._update_view_toolbar_visibility()
        self._update_nav_buttons()

        self._reload()

    def _on_sidebar_current_row_changed(self, row: int) -> None:
        if row < 0 or not self._single_view:
            return
        if row >= len(self._tiles):
            return
        self._stack.setCurrentIndex(row)

    def _sidebar_thumb_label_at(self, row: int) -> QLabel | None:
        it = self._camera_sidebar.item(row)
        if it is None:
            return None
        w = self._camera_sidebar.itemWidget(it)
        if w is None:
            return None
        return w.findChild(QLabel, "sidebarThumb")

    def _sync_sidebar_with_stack(self) -> None:
        if not self._single_view or not self._tiles:
            return
        idx = self._stack.currentIndex()
        if idx < 0 or idx >= self._camera_sidebar.count():
            return
        self._camera_sidebar.blockSignals(True)
        try:
            self._camera_sidebar.setCurrentRow(idx)
        finally:
            self._camera_sidebar.blockSignals(False)

    def _apply_sidebar_thumb_placeholder(self, row: int) -> None:
        if row < 0 or row >= len(self._tiles):
            return
        thumb = self._sidebar_thumb_label_at(row)
        if thumb is None:
            return
        name = self._tiles[row].stream_name
        tw = self._sidebar_thumb_target_width()
        thumb.setPixmap(_sidebar_placeholder_pixmap(name, tw))

    def _sidebar_thumb_target_width(self) -> int:
        vp = self._camera_sidebar.viewport()
        inner = vp.width() - 28
        return max(_SIDEBAR_THUMB_W, inner)

    def _kill_thumb_proc_if_still(self, proc: QProcess) -> None:
        if proc is not self._thumb_proc:
            return
        if proc.state() != QProcess.ProcessState.NotRunning:
            proc.kill()

    def _cancel_thumbnail_cycle(self) -> None:
        self._thumb_queue.clear()
        self._thumb_cycle_active = False
        if self._thumb_proc is not None:
            p = self._thumb_proc
            self._thumb_proc = None
            try:
                p.kill()
            except Exception:
                pass
            p.deleteLater()
        if self._thumb_out_path is not None:
            try:
                self._thumb_out_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._thumb_out_path = None

    def _start_thumbnail_refresh_cycle(self) -> None:
        if not self._single_view or not self._tiles:
            return
        if self._thumb_cycle_active:
            return
        gen = self._thumb_generation
        self._thumb_cycle_active = True
        self._thumb_queue.clear()
        for i, t in enumerate(self._tiles):
            self._thumb_queue.append((i, t.stream_url))
        self._thumb_process_next(gen)

    def _thumb_process_next(self, gen: int) -> None:
        if gen != self._thumb_generation:
            self._thumb_cycle_active = False
            self._thumb_queue.clear()
            return
        exe = shutil.which("ffmpeg")
        if not exe:
            while self._thumb_queue:
                r, _ = self._thumb_queue.popleft()
                self._apply_sidebar_thumb_placeholder(r)
            self._thumb_cycle_active = False
            return
        if not self._thumb_queue:
            self._thumb_cycle_active = False
            return

        row, url = self._thumb_queue.popleft()
        fd, path_str = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        out = Path(path_str)
        self._thumb_out_path = out

        proc = QProcess(self)
        self._thumb_proc = proc
        proc.finished.connect(
            lambda ec, es, g=gen, r=row, p=out: self._on_thumb_proc_finished(ec, es, g, r, p)
        )
        args = [
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            url,
            "-frames:v",
            "1",
            "-q:v",
            "5",
            "-y",
            str(out),
        ]
        try:
            proc.start(exe, args)
        except Exception:
            LOG.exception("Failed starting ffmpeg thumbnail for row %s", row)
            self._thumb_proc = None
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
            self._thumb_out_path = None
            self._apply_sidebar_thumb_placeholder(row)
            QTimer.singleShot(0, lambda: self._thumb_process_next(gen))
            return

        if not proc.waitForStarted(4000):
            LOG.warning("ffmpeg failed to start for sidebar thumbnail row %s", row)
            proc.kill()
            proc.waitForFinished(1000)
            proc.deleteLater()
            self._thumb_proc = None
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
            self._thumb_out_path = None
            self._apply_sidebar_thumb_placeholder(row)
            QTimer.singleShot(0, lambda: self._thumb_process_next(gen))
            return

        QTimer.singleShot(8000, lambda p=proc: self._kill_thumb_proc_if_still(p))

    def _on_thumb_proc_finished(
        self,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
        gen: int,
        row: int,
        out: Path,
    ) -> None:
        if self._thumb_proc is not None:
            self._thumb_proc.deleteLater()
            self._thumb_proc = None
        self._thumb_out_path = None

        if gen != self._thumb_generation:
            self._thumb_cycle_active = False
            self._thumb_queue.clear()
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
            return

        usable = out.is_file() and out.stat().st_size > 0
        try:
            if usable:
                img = QImage(str(out))
                if not img.isNull():
                    tw = self._sidebar_thumb_target_width()
                    pm_src = QPixmap.fromImage(img)
                    pm = _fit_sidebar_thumb_pixmap(
                        pm_src, tw, _SIDEBAR_THUMB_H
                    )
                    if 0 <= row < self._camera_sidebar.count():
                        thumb = self._sidebar_thumb_label_at(row)
                        if thumb is not None:
                            thumb.setPixmap(pm)
                else:
                    self._apply_sidebar_thumb_placeholder(row)
            else:
                self._apply_sidebar_thumb_placeholder(row)
        finally:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass

        QTimer.singleShot(0, lambda: self._thumb_process_next(gen))

    def _rebuild_camera_sidebar(self) -> None:
        self._cancel_thumbnail_cycle()
        self._thumb_generation += 1
        self._camera_sidebar.clear()
        for t in self._tiles:
            item = QListWidgetItem()
            item.setSizeHint(QSize(248, _SIDEBAR_THUMB_H + 40))
            row_w = QWidget()
            row_w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            v = QVBoxLayout(row_w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(4)
            tw = self._sidebar_thumb_target_width()
            thumb = QLabel()
            thumb.setObjectName("sidebarThumb")
            thumb.setMinimumHeight(_SIDEBAR_THUMB_H)
            thumb.setMaximumHeight(_SIDEBAR_THUMB_H)
            thumb.setMinimumWidth(1)
            thumb.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            thumb.setScaledContents(True)
            thumb.setStyleSheet("background: #000; border: 1px solid #333;")
            thumb.setPixmap(_sidebar_placeholder_pixmap(t.stream_name, tw))
            thumb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            name_lbl = QLabel(t.stream_name)
            name_lbl.setStyleSheet("color: #ccc; font-size: 11px;")
            name_lbl.setWordWrap(True)
            name_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            v.addWidget(thumb)
            v.addWidget(name_lbl)
            self._camera_sidebar.addItem(item)
            self._camera_sidebar.setItemWidget(item, row_w)

    def _update_thumbnail_timer_for_mode(self) -> None:
        if self._single_view and self._tiles:
            if not self._thumb_refresh_timer.isActive():
                self._thumb_refresh_timer.start()
            QTimer.singleShot(200, self._start_thumbnail_refresh_cycle)
        else:
            self._thumb_refresh_timer.stop()
            self._cancel_thumbnail_cycle()

    def _edit_configuration(self) -> None:
        path = resolve_config_path()
        saved, _viewer_changed = open_config_editor(self, path)
        if saved:
            self._reload()

    def _mode_hint(self) -> str:
        if self._subprocess:
            return "mpv subprocess"
        return "libmpv"

    def _setup_shortcuts(self) -> None:
        ctx = Qt.ShortcutContext.WindowShortcut

        sc_r = QShortcut(QKeySequence(Qt.Key.Key_Right), self)
        sc_r.setContext(ctx)
        sc_r.activated.connect(self._single_next)
        sc_d = QShortcut(QKeySequence(Qt.Key.Key_Down), self)
        sc_d.setContext(ctx)
        sc_d.activated.connect(self._single_next)
        sc_l = QShortcut(QKeySequence(Qt.Key.Key_Left), self)
        sc_l.setContext(ctx)
        sc_l.activated.connect(self._single_prev)
        sc_u = QShortcut(QKeySequence(Qt.Key.Key_Up), self)
        sc_u.setContext(ctx)
        sc_u.activated.connect(self._single_prev)

        sc_g = QShortcut(QKeySequence(Qt.Key.Key_G), self)
        sc_g.setContext(ctx)
        sc_g.activated.connect(self._toggle_single_view_shortcut)

        sc_f11 = QShortcut(QKeySequence("F11"), self)
        sc_f11.setContext(ctx)
        sc_f11.activated.connect(self._toggle_fullscreen)

        self._shortcut_esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._shortcut_esc.setContext(ctx)
        self._shortcut_esc.activated.connect(self._exit_fullscreen_if_needed)

    def _toggle_single_view_shortcut(self) -> None:
        if not self._tiles:
            return
        self._set_single_view(not self._single_view)

    def _exit_fullscreen_if_needed(self) -> None:
        if self.isFullScreen():
            self.showNormal()

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _sync_view_buttons(self) -> None:
        self._btn_grid.blockSignals(True)
        self._btn_single.blockSignals(True)
        self._btn_grid.setChecked(not self._single_view)
        self._btn_single.setChecked(self._single_view)
        self._btn_grid.blockSignals(False)
        self._btn_single.blockSignals(False)

    def _set_single_view(self, single: bool) -> None:
        if not self._tiles:
            return
        if single == self._single_view:
            self._sync_view_buttons()
            self._update_view_toolbar_visibility()
            return
        if self._single_view and self._stack.count():
            self._single_index = self._stack.currentIndex()
        self._single_view = single
        self._sync_view_buttons()
        self._place_tiles_for_current_mode()
        QTimer.singleShot(0, self._refresh_tiles)

    def _persist_viewer_state(self) -> None:
        stream: str | None = None
        if self._tiles:
            if self._single_view and self._stack.count():
                i = self._stack.currentIndex()
                if 0 <= i < len(self._tiles):
                    stream = self._tiles[i].stream_name
            elif 0 <= self._single_index < len(self._tiles):
                stream = self._tiles[self._single_index].stream_name
        data = {
            "last_single_stream": stream,
            "single_view": bool(self._single_view),
        }
        _save_viewer_state_file(data)
        self._viewer_state = data

    def _detach_tiles_from_layouts(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        while self._stack.count():
            w = self._stack.widget(0)
            self._stack.removeWidget(w)

    def _place_tiles_for_current_mode(self) -> None:
        # Reparenting emits currentChanged for each removeWidget(0); that used to clobber
        # _single_index before we could persist the camera chosen in Single view.
        self._stack.blockSignals(True)
        try:
            self._detach_tiles_from_layouts()
            if not self._tiles:
                self._scroll.setVisible(True)
                self._single_view_host.setVisible(False)
            elif self._single_view:
                for t in self._tiles:
                    self._stack.addWidget(t)
                n = len(self._tiles)
                idx = min(max(self._single_index, 0), n - 1)
                self._stack.setCurrentIndex(idx)
                self._single_index = idx
                self._scroll.setVisible(False)
                self._single_view_host.setVisible(True)
            else:
                cols = 2 if len(self._tiles) <= 4 else 3
                for i, t in enumerate(self._tiles):
                    r, c = divmod(i, cols)
                    self._grid.addWidget(t, r, c)
                # QStackedWidget hides non-current pages; those widgets stay hidden when reparented.
                for t in self._tiles:
                    t.show()
                self._grid_host.updateGeometry()
                self._scroll.updateGeometry()
                self._scroll.setVisible(True)
                self._single_view_host.setVisible(False)
        finally:
            self._stack.blockSignals(False)

        if self._single_view and self._stack.count():
            self._single_index = self._stack.currentIndex()

        self._update_nav_buttons()
        self._update_view_toolbar_visibility()
        self._refresh_status_text()
        self._persist_viewer_state()
        self._sync_single_view_audio_mute()
        self._sync_sidebar_with_stack()
        self._update_thumbnail_timer_for_mode()

    def _sync_single_view_audio_mute(self) -> None:
        if not self._tiles:
            return
        if not self._single_view:
            for t in self._tiles:
                t.set_single_stack_mute_suppressed(False)
            return
        idx = self._stack.currentIndex()
        if idx < 0:
            idx = 0
        for i, t in enumerate(self._tiles):
            t.set_single_stack_mute_suppressed(i != idx)

    def _update_nav_buttons(self) -> None:
        en = self._single_view and len(self._tiles) > 1
        self._btn_prev.setEnabled(en)
        self._btn_next.setEnabled(en)

    def _update_view_toolbar_visibility(self) -> None:
        if self._single_view:
            self._btn_grid.setVisible(True)
            self._btn_single.setVisible(False)
            self._btn_prev.setVisible(True)
            self._btn_next.setVisible(True)
        else:
            self._btn_grid.setVisible(False)
            self._btn_single.setVisible(True)
            self._btn_prev.setVisible(False)
            self._btn_next.setVisible(False)

    def _on_stack_index_changed(self, index: int) -> None:
        if self._single_view and index >= 0:
            self._single_index = index
        self._sync_sidebar_with_stack()
        self._sync_single_view_audio_mute()
        self._refresh_status_text()
        self._persist_viewer_state()

    def _single_next(self) -> None:
        if not self._single_view or len(self._tiles) < 2:
            return
        i = self._stack.currentIndex()
        self._stack.setCurrentIndex((i + 1) % len(self._tiles))

    def _single_prev(self) -> None:
        if not self._single_view or len(self._tiles) < 2:
            return
        i = self._stack.currentIndex()
        self._stack.setCurrentIndex((i - 1) % len(self._tiles))

    def _refresh_status_text(self) -> None:
        if not self._status_base:
            return
        if self._single_view and self._tiles:
            i = self._stack.currentIndex()
            if 0 <= i < len(self._tiles):
                name = self._tiles[i].stream_name
                self._status.setText(
                    f"{i + 1}/{len(self._tiles)} — {name}  |  {self._status_base}"
                )
            else:
                self._status.setText(self._status_base)
        else:
            self._status.setText(self._status_base)

    def _clear_grid(self) -> None:
        self._thumb_refresh_timer.stop()
        self._cancel_thumbnail_cycle()
        self._thumb_generation += 1
        self._camera_sidebar.clear()
        for t in self._tiles:
            t.shutdown()
        self._tiles.clear()
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        while self._stack.count():
            w = self._stack.widget(0)
            self._stack.removeWidget(w)
            w.deleteLater()

    def _reload(self) -> None:
        path = resolve_config_path()
        self._subprocess = _use_mpv_subprocess()
        LOG.info("Reloading UI streams from config: %s", path)

        prev_name: str | None = None
        if self._single_view and self._stack.count():
            cw = self._stack.currentWidget()
            if isinstance(cw, StreamTile):
                prev_name = cw.stream_name
        elif self._tiles and 0 <= self._single_index < len(self._tiles):
            prev_name = self._tiles[self._single_index].stream_name

        self._clear_grid()
        if not path.is_file():
            cfg_dir = app_config_dir()
            self._status_base = ""
            self._status.setText(
                f"No config — create {path} (optional secrets in {cfg_dir / '.env.enc'})"
            )
            self._place_tiles_for_current_mode()
            LOG.warning("Config file not found: %s", path)
            return
        try:
            streams = load_streams(path)
        except Exception as e:
            self._status_base = ""
            self._status.setText(f"Config error: {e}")
            QMessageBox.warning(self, "Config", str(e))
            self._place_tiles_for_current_mode()
            LOG.exception("Failed loading streams from config: %s", path)
            return

        names = ordered_stream_names(path, streams)
        for name in names:
            url = streams[name]
            tile = StreamTile(name, url, subprocess=self._subprocess)
            self._tiles.append(tile)
        self._rebuild_camera_sidebar()
        LOG.info("Loaded %d stream tiles", len(self._tiles))

        if prev_name in names:
            self._single_index = names.index(prev_name)
        else:
            persisted = self._viewer_state.get("last_single_stream")
            if isinstance(persisted, str) and persisted in names:
                self._single_index = names.index(persisted)
            elif names:
                self._single_index = min(self._single_index, len(names) - 1)
            else:
                self._single_index = 0

        hw = _mpv_hwdec()
        self._status_base = (
            f"{len(names)} streams — {path} — {self._mode_hint()} hwdec={hw}"
        )
        self._place_tiles_for_current_mode()
        QTimer.singleShot(0, self._refresh_tiles)

    def _refresh_tiles(self) -> None:
        for t in self._tiles:
            t.update()
            t.repaint()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        LOG.info("Main window closing")
        self._persist_viewer_state()
        self._clear_grid()
        super().closeEvent(event)


def main() -> None:
    log_path = configure_logging()
    LOG.info("Application startup (argv=%s)", sys.argv)
    LOG.info("Log file path: %s", log_path)
    apply_viewer_from_yaml(resolve_config_path())
    _apply_qt_platform_for_wid_embed()
    _strip_wayland_so_mpv_uses_x11()
    _log_display_env()
    embed_reason = _x11_embed_unavailable_reason()
    if embed_reason:
        LOG.error("Embedded video cannot start in current environment: %s", embed_reason)
    if _mpv_debug_enabled():
        _log_mpv("HIKVISION_DEBUG_MPV=1 — mpv IPC and mute decisions logged to stderr")
    app = QApplication(sys.argv)
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except OSError:
        pass
    app.setStyle("Fusion")
    if _force_dark_mode():
        _apply_fusion_dark_palette(app)
    w = MainWindow()
    w.show()
    code = app.exec()
    LOG.info("Application exited with code %s", code)
    sys.exit(code)


if __name__ == "__main__":
    main()
