#!/usr/bin/env python3
import os
import shutil
import sys

import mpv
from PyQt6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from hikvision_viewer.config_editor import open_config_editor
from hikvision_viewer.config_loader import (
    app_config_dir,
    apply_viewer_from_yaml,
    load_streams,
    resolve_config_path,
    resolve_plain_dotenv_path,
)
from hikvision_viewer.env_secure import KeyringError, encrypt_dotenv_move_plain


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _mpv_hwdec() -> str:
    # Multiple embedded players + hwdec often segfaults GPU drivers; opt in with HIKVISION_MPV_HWDEC=auto
    return os.environ.get("HIKVISION_MPV_HWDEC", "no").strip() or "no"


def _mpv_vo() -> str:
    return os.environ.get("HIKVISION_MPV_VO", "gpu").strip() or "gpu"


def _use_mpv_subprocess() -> bool:
    return _env_flag("HIKVISION_MPV_SUBPROCESS", "1")


def _apply_qt_platform_for_wid_embed() -> None:
    """mpv --wid (and libmpv wid) need a real X11 window id; Qt on Wayland does not provide one."""
    if not sys.platform.startswith("linux"):
        return
    if _env_flag("HIKVISION_QT_WAYLAND", "0"):
        return
    if not os.environ.get("WAYLAND_DISPLAY"):
        return
    qpa = (os.environ.get("QT_QPA_PLATFORM") or "").strip().lower()
    if qpa and qpa not in ("wayland", ""):
        return
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


def _mpv_subprocess_environment() -> QProcessEnvironment:
    env = QProcessEnvironment.systemEnvironment()
    if sys.platform.startswith("linux") and not _env_flag("HIKVISION_QT_WAYLAND", "0"):
        if (os.environ.get("QT_QPA_PLATFORM") or "").strip().lower() == "xcb":
            env.remove("WAYLAND_DISPLAY")
            env.remove("WAYLAND_SOCKET")
    return env


_LAVF_RECONNECT = "reconnect_streamed=1,reconnect_delay_max=5"


class StreamTile(QWidget):
    """One camera: label + native surface. Drives mpv either via QProcess (default) or embedded libmpv."""

    def __init__(self, title: str, url: str, subprocess: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._url = url
        self._subprocess = subprocess
        self._player: mpv.MPV | None = None
        self._proc: QProcess | None = None
        self._started = False

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self._label = QLabel(title)
        self._label.setStyleSheet("color: #ccc; font-size: 12px;")
        layout.addWidget(self._label)

        self._surface = QWidget()
        self._surface.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self._surface.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self._surface.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._surface.setMinimumSize(280, 158)
        self._surface.setStyleSheet("background: #000;")
        layout.addWidget(self._surface, stretch=1)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._started:
            return
        self._started = True
        # Map the X11 window before mpv attaches; subprocess needs a real mapped wid.
        delay_ms = 150 if self._subprocess else 0
        QTimer.singleShot(delay_ms, self._start_player)

    def _start_player(self) -> None:
        if not self._surface.isVisible():
            return
        try:
            wid = int(self._surface.winId())
        except Exception:
            self._label.setText(f"{self._title} (no window id)")
            return
        if self._subprocess:
            self._start_subprocess(wid)
        else:
            self._start_libmpv(wid)

    def _start_subprocess(self, wid: int) -> None:
        exe = shutil.which("mpv")
        if not exe:
            self._label.setText(f"{self._title} (mpv not in PATH)")
            return
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.setProcessEnvironment(_mpv_subprocess_environment())
        hwdec, vo = _mpv_hwdec(), _mpv_vo()
        args = [
            "--no-terminal",
            "--mute",
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
        # vo=gpu still picks Wayland EGL if WAYLAND_DISPLAY is set; we strip it above
        # and pin the GPU context when using X11 embed.
        if sys.platform.startswith("linux") and vo in ("gpu", "gpu-next"):
            args.append("--gpu-context=x11egl")
        args.append(self._url)
        proc.finished.connect(self._on_proc_finished)
        proc.errorOccurred.connect(self._on_proc_error)
        self._proc = proc
        proc.start(exe, args)

    def _on_proc_error(self, err: QProcess.ProcessError) -> None:
        self._label.setText(f"{self._title} (mpv start error: {err.name})")

    def _on_proc_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        if self._proc is None:
            return
        if status == QProcess.ExitStatus.CrashExit:
            self._label.setText(f"{self._title} (mpv crashed)")
        elif code != 0:
            self._label.setText(f"{self._title} (mpv exited {code})")

    def _start_libmpv(self, wid: int) -> None:
        vo = _mpv_vo()
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
        )
        if sys.platform.startswith("linux") and vo in ("gpu", "gpu-next"):
            opts["gpu_context"] = "x11egl"
        self._player = mpv.MPV(**opts)
        self._player.play(self._url)

    def shutdown(self) -> None:
        if self._proc is not None:
            if self._proc.state() != QProcess.ProcessState.NotRunning:
                self._proc.terminate()
                if not self._proc.waitForFinished(2500):
                    self._proc.kill()
                    self._proc.waitForFinished(1500)
            self._proc.deleteLater()
            self._proc = None
        if self._player is not None:
            try:
                self._player.terminate()
            except Exception:
                pass
            self._player = None
        self._started = False


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Hikvision RTSP viewer")
        self.resize(1280, 720)
        self._tiles: list[StreamTile] = []
        self._subprocess = _use_mpv_subprocess()

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        bar = QHBoxLayout()
        self._status = QLabel()
        self._status.setStyleSheet("color: #888;")
        bar.addWidget(self._status)
        bar.addStretch()
        reload_btn = QPushButton("Reload config")
        reload_btn.clicked.connect(self._reload)
        bar.addWidget(reload_btn)
        enc_btn = QPushButton("Encrypt env…")
        enc_btn.setToolTip(
            "Move plaintext .env to .env.enc using the OS keyring (Secret Service / "
            "Keychain / Credential Manager) to hold the encryption key."
        )
        enc_btn.clicked.connect(self._encrypt_env)
        bar.addWidget(enc_btn)
        edit_btn = QPushButton("Edit configuration…")
        edit_btn.setToolTip(
            "Edit streams, Hikvision URL builder, playback options, and .env. "
            "Playback (viewer:) changes need an app restart to apply fully."
        )
        edit_btn.clicked.connect(self._edit_configuration)
        bar.addWidget(edit_btn)
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

        self._reload()

    def _edit_configuration(self) -> None:
        path = resolve_config_path()
        saved, _viewer_changed = open_config_editor(self, path)
        if saved:
            self._reload()

    def _mode_hint(self) -> str:
        if self._subprocess:
            return "mpv subprocess"
        return "libmpv"

    def _clear_grid(self) -> None:
        for t in self._tiles:
            t.shutdown()
        self._tiles.clear()
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _encrypt_env(self) -> None:
        cfg = resolve_config_path()
        dotenv_path = resolve_plain_dotenv_path(cfg)
        if dotenv_path is None:
            QMessageBox.information(
                self,
                "Encrypt env",
                "No plaintext .env found next to the config file or under the app config directory.",
            )
            return
        try:
            enc_path = encrypt_dotenv_move_plain(dotenv_path)
        except KeyringError as e:
            QMessageBox.warning(
                self,
                "Encrypt env",
                f"Could not use the OS keyring: {e}\n\n"
                "On Linux, install a Secret Service provider (e.g. gnome-keyring or KWallet) "
                "and python-secretstorage if needed.",
            )
            return
        except OSError as e:
            QMessageBox.warning(self, "Encrypt env", str(e))
            return
        QMessageBox.information(
            self,
            "Encrypt env",
            f"Plaintext removed.\nSecrets are now in:\n{enc_path}\n\n"
            "The encryption key is stored only in your OS keyring (service "
            '"hikvision-viewer"). Back up .env.enc and keep keyring access on this machine.',
        )
        self._reload()

    def _reload(self) -> None:
        path = resolve_config_path()
        self._subprocess = _use_mpv_subprocess()
        self._clear_grid()
        if not path.is_file():
            cfg_dir = app_config_dir()
            self._status.setText(
                f"No config — create {path} (optional secrets in {cfg_dir / '.env'} or .env.enc)"
            )
            return
        try:
            streams = load_streams(path)
        except Exception as e:
            self._status.setText(f"Config error: {e}")
            QMessageBox.warning(self, "Config", str(e))
            return

        names = sorted(streams.keys())
        cols = 2 if len(names) <= 4 else 3
        for i, name in enumerate(names):
            url = streams[name]
            tile = StreamTile(name, url, subprocess=self._subprocess)
            r, c = divmod(i, cols)
            self._grid.addWidget(tile, r, c)
            self._tiles.append(tile)

        hw = _mpv_hwdec()
        self._status.setText(
            f"{len(names)} streams — {path} — {self._mode_hint()} hwdec={hw}"
        )

        QTimer.singleShot(0, self._refresh_tiles)

    def _refresh_tiles(self) -> None:
        for t in self._tiles:
            t.update()
            t.repaint()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._clear_grid()
        super().closeEvent(event)


def main() -> None:
    apply_viewer_from_yaml(resolve_config_path())
    _apply_qt_platform_for_wid_embed()
    _strip_wayland_so_mpv_uses_x11()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
