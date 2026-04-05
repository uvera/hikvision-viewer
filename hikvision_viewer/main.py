#!/usr/bin/env python3
import json
import os
import shutil
import sys

import mpv
from PyQt6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
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
    resolve_plain_dotenv_path,
)
from hikvision_viewer.env_secure import KeyringError, encrypt_dotenv_move_plain


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


def _use_mpv_subprocess() -> bool:
    # PyInstaller/AppImage ships libmpv, not the `mpv` binary; subprocess mode needs mpv on PATH.
    default = "0" if getattr(sys, "frozen", False) else "1"
    return _env_flag("HIKVISION_MPV_SUBPROCESS", default)


def _force_dark_mode() -> bool:
    return _env_flag("HIKVISION_FORCE_DARK", "0")


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

    @property
    def stream_name(self) -> str:
        return self._title

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
        act_encrypt = QAction("Encrypt env…", self)
        act_encrypt.setToolTip(
            "Move plaintext .env to .env.enc using the OS keyring (Secret Service / "
            "Keychain / Credential Manager) to hold the encryption key."
        )
        act_encrypt.triggered.connect(self._encrypt_env)
        settings_menu.addAction(act_encrypt)
        act_edit = QAction("Edit configuration…", self)
        act_edit.setToolTip(
            "Edit streams, Hikvision URL builder, playback options, and .env. "
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

        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: #111;")
        self._stack.currentChanged.connect(self._on_stack_index_changed)
        outer.addWidget(self._stack, stretch=1)
        self._stack.hide()

        self._setup_shortcuts()
        self._update_view_toolbar_visibility()
        self._update_nav_buttons()

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
                self._stack.setVisible(False)
            elif self._single_view:
                for t in self._tiles:
                    self._stack.addWidget(t)
                n = len(self._tiles)
                idx = min(max(self._single_index, 0), n - 1)
                self._stack.setCurrentIndex(idx)
                self._single_index = idx
                self._scroll.setVisible(False)
                self._stack.setVisible(True)
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
                self._stack.setVisible(False)
        finally:
            self._stack.blockSignals(False)

        if self._single_view and self._stack.count():
            self._single_index = self._stack.currentIndex()

        self._update_nav_buttons()
        self._update_view_toolbar_visibility()
        self._refresh_status_text()
        self._persist_viewer_state()

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
                f"No config — create {path} (optional secrets in {cfg_dir / '.env'} or .env.enc)"
            )
            self._place_tiles_for_current_mode()
            return
        try:
            streams = load_streams(path)
        except Exception as e:
            self._status_base = ""
            self._status.setText(f"Config error: {e}")
            QMessageBox.warning(self, "Config", str(e))
            self._place_tiles_for_current_mode()
            return

        names = ordered_stream_names(path, streams)
        for name in names:
            url = streams[name]
            tile = StreamTile(name, url, subprocess=self._subprocess)
            self._tiles.append(tile)

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
        self._persist_viewer_state()
        self._clear_grid()
        super().closeEvent(event)


def main() -> None:
    apply_viewer_from_yaml(resolve_config_path())
    _apply_qt_platform_for_wid_embed()
    _strip_wayland_so_mpv_uses_x11()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    if _force_dark_mode():
        _apply_fusion_dark_palette(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
