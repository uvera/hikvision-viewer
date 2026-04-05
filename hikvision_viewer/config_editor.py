"""PyQt6 configuration editor: streams (Hikvision URL builder), playback (viewer:), .env."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from hikvision_viewer.config_loader import (
    app_config_dir,
    load_config_document,
    ordered_stream_names,
    parse_streams_raw,
    resolve_plain_dotenv_path,
    save_config_document,
    streams_to_yaml_entries,
)
from hikvision_viewer.env_secure import (
    KeyringError,
    decrypt_env_file_to_str,
    encrypt_plaintext_to_path,
)
from hikvision_viewer.hikvision_rtsp import (
    build_hikvision_rtsp_url,
    try_parse_hikvision_rtsp_url,
)


@dataclass
class StreamRow:
    name: str
    use_hikvision: bool = True
    url_custom: str = ""
    hv_user: str = "admin"
    hv_password: str = ""
    hv_host: str = ""
    hv_port: int = 554
    hv_channel: int = 101


class ConfigEditorDialog(QDialog):
    def __init__(self, config_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config_path = config_path
        self._saved = False
        self._viewer_changed = False
        self._initial_viewer_yaml: dict | None = None
        self._rows: list[StreamRow] = []
        self._loading_ui = False
        self._prev_list_row = -1
        self._env_target_plain: Path | None = None
        self._env_target_enc: Path | None = None

        self.setWindowTitle("Edit configuration")
        self.resize(920, 640)

        root = QVBoxLayout(self)
        self._tabs = QTabWidget()
        root.addWidget(self._tabs)

        self._streams_tab = QWidget()
        self._tabs.addTab(self._streams_tab, "Streams")
        self._build_streams_tab()

        self._playback_tab = QWidget()
        self._tabs.addTab(self._playback_tab, "Playback")
        self._build_playback_tab()

        self._env_tab = QWidget()
        self._tabs.addTab(self._env_tab, "Environment")
        self._build_env_tab()

        self._tabs.currentChanged.connect(self._on_main_tab_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._btn_revert = QPushButton("Revert")
        buttons.addButton(self._btn_revert, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        self._btn_revert.clicked.connect(self._reload_from_disk)
        root.addWidget(buttons)

        self._reload_from_disk()

    def saved(self) -> bool:
        return self._saved

    def viewer_changed(self) -> bool:
        return self._viewer_changed

    def _build_streams_tab(self) -> None:
        layout = QVBoxLayout(self._streams_tab)
        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split)

        left = QWidget()
        ll = QVBoxLayout(left)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.currentRowChanged.connect(self._on_list_row_changed)
        ll.addWidget(QLabel("Streams"))
        ll.addWidget(self._list)
        hb = QHBoxLayout()
        b_add = QPushButton("Add")
        b_add.clicked.connect(self._add_stream)
        b_rm = QPushButton("Remove")
        b_rm.clicked.connect(self._remove_stream)
        hb.addWidget(b_add)
        hb.addWidget(b_rm)
        ll.addLayout(hb)
        split.addWidget(left)

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_inner = QWidget()
        right_scroll.setWidget(right_inner)
        rl = QVBoxLayout(right_inner)

        self._name_edit = QLineEdit()
        self._name_edit.textChanged.connect(self._on_name_changed)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("URL mode:"))
        self._mode_hik = QRadioButton("Hikvision")
        self._mode_custom = QRadioButton("Custom URL")
        self._mode_hik.toggled.connect(self._on_mode_toggled)
        mode_row.addWidget(self._mode_hik)
        mode_row.addWidget(self._mode_custom)
        mode_row.addStretch()
        rl.addWidget(QLabel("Stream name"))
        rl.addWidget(self._name_edit)
        rl.addLayout(mode_row)

        self._stack = QStackedWidget()
        rl.addWidget(self._stack, stretch=1)

        hik_frame = QFrame()
        hik_frame.setObjectName("hikvisionCard")
        hik_frame.setStyleSheet(
            "#hikvisionCard { background: #2a2a2a; border: 1px solid #444; "
            "border-radius: 6px; padding: 8px; }"
        )
        hf = QVBoxLayout(hik_frame)
        hf.addWidget(QLabel("Hikvision RTSP"))
        form = QFormLayout()
        self._hv_user = QLineEdit()
        self._hv_user.setPlaceholderText("admin")
        self._hv_password = QLineEdit()
        self._hv_password.setPlaceholderText("{CAM_PASSWORD} or literal")
        self._hv_host = QLineEdit()
        self._hv_host.setPlaceholderText("192.168.1.10 or {CAM_IP}")
        self._hv_port = QSpinBox()
        self._hv_port.setRange(1, 65535)
        self._hv_port.setValue(554)
        ch_row = QHBoxLayout()
        self._ch_main = QRadioButton("Main (channel 101)")
        self._ch_sub = QRadioButton("Sub (channel 102)")
        self._ch_main.setChecked(True)
        self._ch_main.toggled.connect(self._on_hik_field_changed)
        self._ch_sub.toggled.connect(self._on_hik_field_changed)
        ch_row.addWidget(self._ch_main)
        ch_row.addWidget(self._ch_sub)
        ch_row.addStretch()
        self._hv_user.textChanged.connect(self._on_hik_field_changed)
        self._hv_password.textChanged.connect(self._on_hik_field_changed)
        self._hv_host.textChanged.connect(self._on_hik_field_changed)
        self._hv_port.valueChanged.connect(self._on_hik_field_changed)

        form.addRow("Username", self._hv_user)
        form.addRow("Password", self._hv_password)
        form.addRow("Host / IP", self._hv_host)
        form.addRow("Port", self._hv_port)
        form.addRow("Stream", ch_row)
        hf.addLayout(form)
        prev_label = QLabel("Preview")
        prev_label.setStyleSheet("color: #aaa; margin-top: 8px;")
        hf.addWidget(prev_label)
        self._url_preview = QLabel()
        self._url_preview.setWordWrap(True)
        self._url_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._url_preview.setStyleSheet(
            "font-family: monospace; font-size: 11px; color: #e0e0e0; "
            "background: #111; padding: 8px; border-radius: 4px;"
        )
        hf.addWidget(self._url_preview)
        self._stack.addWidget(hik_frame)

        custom_w = QWidget()
        cv = QVBoxLayout(custom_w)
        cv.addWidget(QLabel("RTSP / URL"))
        self._custom_url = QLineEdit()
        self._custom_url.textChanged.connect(self._on_custom_url_changed)
        cv.addWidget(self._custom_url)
        cv.addStretch()
        self._stack.addWidget(custom_w)

        split.addWidget(right_scroll)
        split.setStretchFactor(1, 2)

    def _build_playback_tab(self) -> None:
        lay = QVBoxLayout(self._playback_tab)
        box = QGroupBox("Saved to config.yaml under viewer: (restart app to apply)")
        fl = QVBoxLayout(box)
        self._pb_sub = QCheckBox("Use mpv subprocess (one process per tile)")
        self._pb_sub.setChecked(True)
        fl.addWidget(self._pb_sub)
        hw = QHBoxLayout()
        hw.addWidget(QLabel("Hardware decode (mpv --hwdec)"))
        fl.addLayout(hw)
        self._pb_hwdec = QComboBox()
        self._pb_hwdec.addItems(["no", "auto", "yes", "vdpau", "vaapi"])
        fl.addWidget(self._pb_hwdec)
        vo = QHBoxLayout()
        vo.addWidget(QLabel("Video output (mpv --vo)"))
        fl.addLayout(vo)
        self._pb_vo = QLineEdit()
        self._pb_vo.setPlaceholderText("gpu")
        fl.addWidget(self._pb_vo)
        self._pb_wayland = QCheckBox(
            "Allow native Qt Wayland (disables RTSP embedding on many setups)"
        )
        fl.addWidget(self._pb_wayland)
        self._pb_dark = QCheckBox("Force dark mode (Fusion palette; ignores system theme)")
        fl.addWidget(self._pb_dark)
        hint = QLabel(
            "Non-empty HIKVISION_* environment variables already set when the app "
            "starts override these values."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        fl.addWidget(hint)
        lay.addWidget(box)

        order_box = QGroupBox(
            "Single view camera order (Prev/Next, saved as viewer.single_view_order)"
        )
        ol = QVBoxLayout(order_box)
        order_hint = QLabel(
            "Drag items to set cycle order. Missing or new streams are appended "
            "alphabetically when you save. Switch to this tab to refresh the list "
            "after renaming streams."
        )
        order_hint.setWordWrap(True)
        order_hint.setStyleSheet("color: #888;")
        ol.addWidget(order_hint)
        self._pb_order_list = QListWidget()
        self._pb_order_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._pb_order_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._pb_order_list.setMinimumHeight(160)
        ol.addWidget(self._pb_order_list)
        lay.addWidget(order_box)
        lay.addStretch()

    def _build_env_tab(self) -> None:
        lay = QVBoxLayout(self._env_tab)
        self._env_info = QLabel()
        self._env_info.setWordWrap(True)
        self._env_info.setStyleSheet("color: #888;")
        lay.addWidget(self._env_info)
        self._env_edit = QPlainTextEdit()
        lay.addWidget(self._env_edit)

    def _reload_from_disk(self) -> None:
        self._loading_ui = True
        data = load_config_document(self._config_path)
        raw_streams = parse_streams_raw(data)
        self._rows = [self._row_from_url(n, u) for n, u in raw_streams.items()]

        if not self._rows:
            self._rows.append(StreamRow(name="camera_1"))

        viewer = data.get("viewer") if isinstance(data.get("viewer"), dict) else {}
        self._initial_viewer_yaml = dict(viewer) if viewer else {}
        self._pb_sub.setChecked(bool(viewer.get("mpv_subprocess", True)))
        hw = str(viewer.get("mpv_hwdec", "no") or "no")
        i = self._pb_hwdec.findText(hw)
        self._pb_hwdec.setCurrentIndex(i if i >= 0 else 0)
        self._pb_vo.setText(str(viewer.get("mpv_vo", "gpu") or "gpu"))
        self._pb_wayland.setChecked(bool(viewer.get("qt_wayland", False)))
        self._pb_dark.setChecked(bool(viewer.get("force_dark_mode", False)))

        self._refresh_list_widget()
        self._prev_list_row = -1
        self._list.setCurrentRow(0)
        self._prev_list_row = 0
        self._load_row_into_ui(0)

        self._env_target_plain = None
        self._env_target_enc = None
        plain = resolve_plain_dotenv_path(self._config_path)
        enc = self._first_existing_env_enc()
        if plain is not None:
            self._env_edit.setPlainText(plain.read_text(encoding="utf-8"))
            self._env_edit.setEnabled(True)
            self._env_target_plain = plain
            self._env_info.setText(f"Editing plaintext secrets: {plain}")
        elif enc is not None:
            try:
                self._env_edit.setPlainText(
                    decrypt_env_file_to_str(enc)
                )
            except (OSError, RuntimeError) as e:
                self._env_edit.clear()
                self._env_edit.setEnabled(False)
                self._env_info.setText(
                    f"Could not decrypt {enc}: {e}\n\n"
                    "Fix keyring access or restore a plaintext .env backup."
                )
            else:
                self._env_edit.setEnabled(True)
                self._env_target_enc = enc
                self._env_info.setText(
                    f"Editing decrypted secrets (saved back to encrypted file): {enc}\n"
                    "Plaintext stays in memory only until you save; it is not written to disk."
                )
        else:
            self._env_edit.clear()
            self._env_edit.setEnabled(True)
            self._env_info.setText(
                f"No .env yet — saving will create {self._default_new_dotenv_path()} "
                "if you enter content."
            )

        self._loading_ui = False
        self._viewer_changed = False
        self._refresh_single_view_order_list()

    def _on_main_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        w = self._tabs.widget(index)
        if w is not None and w is self._playback_tab:
            self._refresh_single_view_order_list()

    def _refresh_single_view_order_list(self) -> None:
        idx = self._current_row_index()
        if idx >= 0:
            self._sync_ui_to_row(idx)
        streams_map: dict[str, str] = {}
        for r in self._rows:
            n = r.name.strip()
            if n:
                streams_map[n] = ""
        self._pb_order_list.clear()
        if not streams_map:
            return
        for n in ordered_stream_names(self._config_path, streams_map):
            self._pb_order_list.addItem(QListWidgetItem(n))

    def _single_view_order_for_save(self, stream_keys: set[str]) -> list[str]:
        out: list[str] = []
        for i in range(self._pb_order_list.count()):
            it = self._pb_order_list.item(i)
            if it is None:
                continue
            t = it.text().strip()
            if t in stream_keys and t not in out:
                out.append(t)
        for n in sorted(stream_keys):
            if n not in out:
                out.append(n)
        return out

    def _first_existing_env_enc(self) -> Path | None:
        for base in (self._config_path.parent, app_config_dir()):
            p = base / ".env.enc"
            if p.is_file():
                return p
        return None

    def _default_new_dotenv_path(self) -> Path:
        plain = resolve_plain_dotenv_path(self._config_path)
        if plain is not None:
            return plain
        if self._config_path.parent.is_dir():
            return self._config_path.parent / ".env"
        return app_config_dir() / ".env"

    def _row_from_url(self, name: str, url: str) -> StreamRow:
        parts = try_parse_hikvision_rtsp_url(url)
        if parts is not None:
            return StreamRow(
                name=name,
                use_hikvision=True,
                hv_user=parts.user,
                hv_password=parts.password_expr,
                hv_host=parts.host_expr,
                hv_port=parts.port,
                hv_channel=parts.channel,
            )
        return StreamRow(name=name, use_hikvision=False, url_custom=url)

    def _refresh_list_widget(self) -> None:
        self._list.clear()
        for r in self._rows:
            self._list.addItem(QListWidgetItem(r.name or "(unnamed)"))

    def _current_row_index(self) -> int:
        return self._list.currentRow()

    def _sync_ui_to_row(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._rows):
            return
        r = self._rows[idx]
        r.name = self._name_edit.text().strip()
        r.use_hikvision = self._mode_hik.isChecked()
        if r.use_hikvision:
            r.hv_user = self._hv_user.text().strip() or "admin"
            r.hv_password = self._hv_password.text()
            r.hv_host = self._hv_host.text().strip()
            r.hv_port = int(self._hv_port.value())
            r.hv_channel = 101 if self._ch_main.isChecked() else 102
        else:
            r.url_custom = self._custom_url.text().strip()

    def _load_row_into_ui(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._rows):
            return
        r = self._rows[idx]
        self._name_edit.setText(r.name)
        self._mode_hik.setChecked(r.use_hikvision)
        self._mode_custom.setChecked(not r.use_hikvision)
        self._stack.setCurrentIndex(0 if r.use_hikvision else 1)
        self._hv_user.setText(r.hv_user)
        self._hv_password.setText(r.hv_password)
        self._hv_host.setText(r.hv_host)
        self._hv_port.setValue(r.hv_port)
        if r.hv_channel == 102:
            self._ch_sub.setChecked(True)
        else:
            self._ch_main.setChecked(True)
        self._custom_url.setText(r.url_custom)
        self._update_hik_preview()

    def _row_effective_url(self, r: StreamRow) -> str:
        if r.use_hikvision:
            return build_hikvision_rtsp_url(
                r.hv_user,
                r.hv_password,
                r.hv_host,
                port=r.hv_port,
                channel=r.hv_channel,
            )
        return r.url_custom.strip()

    def _update_hik_preview(self) -> None:
        if not self._mode_hik.isChecked():
            return
        idx = self._current_row_index()
        if idx < 0:
            self._url_preview.setText("")
            return
        user = self._hv_user.text().strip() or "admin"
        password = self._hv_password.text()
        host = self._hv_host.text().strip()
        port = int(self._hv_port.value())
        ch = 101 if self._ch_main.isChecked() else 102
        self._url_preview.setText(
            build_hikvision_rtsp_url(user, password, host, port=port, channel=ch)
        )

    def _on_list_row_changed(self, row: int) -> None:
        if self._loading_ui:
            return
        prev = self._prev_list_row
        if prev >= 0 and prev < len(self._rows):
            self._sync_ui_to_row(prev)
            self._refresh_list_item(prev)
        self._prev_list_row = row
        self._load_row_into_ui(row)

    def _refresh_list_item(self, idx: int) -> None:
        if 0 <= idx < self._list.count():
            it = self._list.item(idx)
            if it is not None:
                n = self._rows[idx].name if idx < len(self._rows) else ""
                it.setText(n or "(unnamed)")

    def _on_name_changed(self, _: str) -> None:
        if self._loading_ui:
            return
        idx = self._current_row_index()
        if idx >= 0:
            self._refresh_list_item(idx)

    def _on_mode_toggled(self, _checked: bool) -> None:
        if self._loading_ui:
            return
        idx = self._current_row_index()
        if idx < 0:
            return
        self._loading_ui = True
        try:
            r = self._rows[idx]
            if self._mode_custom.isChecked():
                user = self._hv_user.text().strip() or "admin"
                password = self._hv_password.text()
                host = self._hv_host.text().strip()
                port = int(self._hv_port.value())
                ch = 101 if self._ch_main.isChecked() else 102
                r.url_custom = build_hikvision_rtsp_url(
                    user, password, host, port=port, channel=ch
                )
                r.use_hikvision = False
                self._custom_url.setText(r.url_custom)
                self._stack.setCurrentIndex(1)
            else:
                r.use_hikvision = True
                parts = try_parse_hikvision_rtsp_url(self._custom_url.text().strip())
                if parts is not None:
                    r.hv_user = parts.user
                    r.hv_password = parts.password_expr
                    r.hv_host = parts.host_expr
                    r.hv_port = parts.port
                    r.hv_channel = parts.channel
                self._hv_user.setText(r.hv_user)
                self._hv_password.setText(r.hv_password)
                self._hv_host.setText(r.hv_host)
                self._hv_port.setValue(r.hv_port)
                if r.hv_channel == 102:
                    self._ch_sub.setChecked(True)
                else:
                    self._ch_main.setChecked(True)
                self._stack.setCurrentIndex(0)
                self._update_hik_preview()
        finally:
            self._loading_ui = False

    def _on_hik_field_changed(self, *_args: object) -> None:
        if self._loading_ui:
            return
        self._update_hik_preview()

    def _on_custom_url_changed(self, *_args: object) -> None:
        if self._loading_ui:
            return
        idx = self._current_row_index()
        if idx >= 0 and self._mode_custom.isChecked():
            self._rows[idx].url_custom = self._custom_url.text()

    def _add_stream(self) -> None:
        idx = self._current_row_index()
        if idx >= 0:
            self._sync_ui_to_row(idx)
        n = 1
        base = "camera"
        names = {r.name for r in self._rows}
        while f"{base}_{n}" in names:
            n += 1
        self._rows.append(StreamRow(name=f"{base}_{n}"))
        self._refresh_list_widget()
        self._list.setCurrentRow(len(self._rows) - 1)

    def _remove_stream(self) -> None:
        idx = self._current_row_index()
        if idx < 0 or not self._rows:
            return
        del self._rows[idx]
        if not self._rows:
            self._rows.append(StreamRow(name="camera_1"))
        self._refresh_list_widget()
        new_i = min(idx, len(self._rows) - 1)
        self._prev_list_row = -1
        self._list.setCurrentRow(new_i)

    def _collect_streams_dict(self) -> dict[str, str]:
        idx = self._current_row_index()
        if idx >= 0:
            self._sync_ui_to_row(idx)
        out: dict[str, str] = {}
        names_seen: set[str] = set()
        for r in self._rows:
            name = r.name.strip()
            if not name:
                raise ValueError("Every stream needs a non-empty name.")
            if name in names_seen:
                raise ValueError(f"Duplicate stream name: {name!r}")
            names_seen.add(name)
            url = self._row_effective_url(r).strip()
            if not url:
                raise ValueError(f"Stream {name!r} needs a URL.")
            out[name] = url
        return out

    def _viewer_dict_from_ui(self, stream_keys: set[str]) -> dict:
        return {
            "mpv_subprocess": self._pb_sub.isChecked(),
            "mpv_hwdec": self._pb_hwdec.currentText().strip() or "no",
            "mpv_vo": self._pb_vo.text().strip() or "gpu",
            "qt_wayland": self._pb_wayland.isChecked(),
            "force_dark_mode": self._pb_dark.isChecked(),
            "single_view_order": self._single_view_order_for_save(stream_keys),
        }

    def _on_save(self) -> None:
        idx = self._current_row_index()
        if idx >= 0:
            self._sync_ui_to_row(idx)
        try:
            streams = self._collect_streams_dict()
        except ValueError as e:
            QMessageBox.warning(self, "Configuration", str(e))
            return

        data = load_config_document(self._config_path)
        data["streams"] = streams_to_yaml_entries(streams)
        viewer = self._viewer_dict_from_ui(set(streams.keys()))
        data["viewer"] = viewer
        self._viewer_changed = viewer != (self._initial_viewer_yaml or {})

        try:
            save_config_document(self._config_path, data)
        except OSError as e:
            QMessageBox.warning(self, "Configuration", f"Could not save YAML: {e}")
            return

        if not self._env_edit.isEnabled():
            pass
        elif self._env_target_plain is not None:
            try:
                self._env_target_plain.parent.mkdir(parents=True, exist_ok=True)
                self._env_target_plain.write_text(
                    self._env_edit.toPlainText(), encoding="utf-8"
                )
            except OSError as e:
                QMessageBox.warning(self, "Environment", f"Could not save .env: {e}")
                return
        elif self._env_target_enc is not None:
            try:
                self._env_target_enc.parent.mkdir(parents=True, exist_ok=True)
                encrypt_plaintext_to_path(
                    self._env_edit.toPlainText(), self._env_target_enc
                )
            except (OSError, RuntimeError, KeyringError) as e:
                QMessageBox.warning(
                    self, "Environment", f"Could not save .env.enc: {e}"
                )
                return
        elif self._env_edit.toPlainText().strip():
            env_path = self._default_new_dotenv_path()
            try:
                env_path.parent.mkdir(parents=True, exist_ok=True)
                env_path.write_text(self._env_edit.toPlainText(), encoding="utf-8")
            except OSError as e:
                QMessageBox.warning(self, "Environment", f"Could not save .env: {e}")
                return

        self._saved = True
        if self._viewer_changed:
            QMessageBox.information(
                self,
                "Configuration",
                "Playback settings were saved. Restart the application for them to take full effect.",
            )
        self.accept()


def open_config_editor(parent: QWidget, config_path: Path) -> tuple[bool, bool]:
    """Show the editor modally. Returns (saved, viewer_changed)."""
    dlg = ConfigEditorDialog(config_path, parent=parent)
    dlg.exec()
    return dlg.saved(), dlg.viewer_changed()
