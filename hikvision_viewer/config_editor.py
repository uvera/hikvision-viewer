"""PyQt6 configuration editor: streams (Hikvision URL builder), playback (viewer:), .env.enc."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
from typing import Literal

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
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from hikvision_viewer.config_loader import (
    StreamYamlSpec,
    app_config_dir,
    load_config_document,
    ordered_stream_names,
    parse_streams_raw,
    save_config_document,
    streams_to_yaml_entries,
)
from hikvision_viewer.env_secure import (
    KeyringError,
    decrypt_env_file_to_str,
    encrypt_plaintext_to_path,
)
from hikvision_viewer.hikvision_rtsp import (
    RtspHikEndpointHints,
    build_hikvision_rtsp_url,
    extract_rtsp_hik_endpoint_hints,
    merge_channel_segment_in_hik_path,
    merge_rtsp_netloc_into_url,
    try_parse_hikvision_rtsp_url,
)

LOG = logging.getLogger(__name__)


@dataclass
class StreamRow:
    """In-memory stream row for the editor. Matches YAML url_type + UI mode."""

    name: str
    url_type: Literal["hikvision", "custom"] = "hikvision"
    #: If True, URL is built from hv_* fields; if False but url_type is hikvision, use url_custom.
    hikvision_structured: bool = True
    url_custom: str = ""
    hv_user: str = "admin"
    hv_password: str = ""
    hv_host: str = ""
    hv_port: str = "554"
    hv_channel: str = "101"


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

        self._hik_card = QFrame()
        self._hik_card.setObjectName("hikvisionCard")
        self._hik_card.setStyleSheet(
            "#hikvisionCard { background: #2a2a2a; border: 1px solid #444; "
            "border-radius: 6px; padding: 8px; }"
        )
        hf = QVBoxLayout(self._hik_card)
        hf.addWidget(QLabel("Hikvision RTSP"))
        form = QFormLayout()
        self._hv_user = QLineEdit()
        self._hv_user.setPlaceholderText("admin")
        self._hv_password = QLineEdit()
        self._hv_password.setPlaceholderText("{CAM_PASSWORD} or literal")
        self._hv_host = QLineEdit()
        self._hv_host.setPlaceholderText("192.168.1.10 or {CAM_IP}")
        self._hv_port = QLineEdit()
        self._hv_port.setPlaceholderText("554 or {NVR_RTSP_PORT}")
        self._hv_channel = QLineEdit()
        self._hv_channel.setPlaceholderText("101 / 301 / {CAM_SIDE_ID}01 / …")
        self._hv_channel.setToolTip(
            "Trailing path segment after /Streaming/Channels/ — digits or env placeholders."
        )
        self._hv_user.textChanged.connect(self._on_hik_field_changed)
        self._hv_password.textChanged.connect(self._on_hik_field_changed)
        self._hv_host.textChanged.connect(self._on_hik_field_changed)
        self._hv_port.textChanged.connect(self._on_hik_field_changed)
        self._hv_channel.textChanged.connect(self._on_hik_field_changed)

        form.addRow("Username", self._hv_user)
        form.addRow("Password", self._hv_password)
        form.addRow("Host / IP", self._hv_host)
        form.addRow("Port", self._hv_port)
        form.addRow("Channel", self._hv_channel)
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
        self._hik_raw_block = QWidget()
        hik_raw_lay = QVBoxLayout(self._hik_raw_block)
        hik_raw_lay.setContentsMargins(0, 8, 0, 0)
        raw_hint = QLabel(
            "Full RTSP URL (placeholders/NVR paths that do not split into numeric channel).\n"
            "Saved URL is taken from here. Leaving the field parses user/host/port into the "
            "fields above when possible (focus out / Tab)."
        )
        raw_hint.setWordWrap(True)
        raw_hint.setStyleSheet("color: #aaa;")
        self._hik_raw_url = QLineEdit()
        self._hik_raw_url.setPlaceholderText(
            "rtsp://{NVR_USER}:{NVR_PASS}@{NVR_IP}:554/Streaming/Channels/…"
        )
        self._hik_raw_url.textChanged.connect(self._on_hik_raw_url_changed)
        self._hik_raw_url.editingFinished.connect(self._on_hik_raw_url_editing_finished)
        hik_raw_lay.addWidget(raw_hint)
        hik_raw_lay.addWidget(self._hik_raw_url)
        hf.addWidget(self._hik_raw_block)
        self._hik_raw_block.setVisible(False)
        self._hik_card.setMinimumHeight(120)
        rl.addWidget(self._hik_card, stretch=1)

        self._url_panel = QWidget()
        url_lay = QVBoxLayout(self._url_panel)
        self._url_panel_title = QLabel("RTSP / URL")
        self._custom_url = QLineEdit()
        self._custom_url.textChanged.connect(self._on_custom_url_changed)
        url_lay.addWidget(self._url_panel_title)
        url_lay.addWidget(self._custom_url)
        url_lay.addStretch()
        rl.addWidget(self._url_panel, stretch=1)

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
        LOG.info("Reloading config editor state from disk: %s", self._config_path)
        self._loading_ui = True
        data = load_config_document(self._config_path)
        raw_streams = parse_streams_raw(data)
        self._rows = [self._row_from_spec(n, s) for n, s in raw_streams.items()]

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

        self._env_target_enc = None
        enc = self._first_existing_env_enc()
        if enc is not None:
            try:
                self._env_edit.setPlainText(
                    decrypt_env_file_to_str(enc)
                )
            except (OSError, RuntimeError) as e:
                self._env_edit.clear()
                self._env_edit.setEnabled(False)
                self._env_info.setText(
                    f"Could not decrypt {enc}: {e}\n\n"
                    "Fix keyring access or restore a .env.enc backup from this machine."
                )
            else:
                self._env_edit.setEnabled(True)
                self._env_target_enc = enc
                self._env_info.setText(
                    f"Editing decrypted secrets (saved encrypted to): {enc}\n"
                    "Plain text exists in memory only until you save."
                )
        else:
            self._env_edit.clear()
            self._env_edit.setEnabled(True)
            self._env_info.setText(
                f"No .env.enc yet — saving with content will create "
                f"{self._default_new_env_enc_path()} (encrypted)."
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

    def _default_new_env_enc_path(self) -> Path:
        if self._config_path.parent.is_dir():
            return self._config_path.parent / ".env.enc"
        return app_config_dir() / ".env.enc"

    def _row_from_spec(self, name: str, spec: StreamYamlSpec) -> StreamRow:
        if spec.url_type == "custom":
            return StreamRow(
                name=name,
                url_type="custom",
                hikvision_structured=False,
                url_custom=spec.url,
            )
        parts = try_parse_hikvision_rtsp_url(spec.url)
        if parts is not None:
            return StreamRow(
                name=name,
                url_type="hikvision",
                hikvision_structured=True,
                hv_user=parts.user,
                hv_password=parts.password_expr,
                hv_host=parts.host_expr,
                hv_port=str(parts.port),
                hv_channel=str(parts.channel),
            )
        hints = extract_rtsp_hik_endpoint_hints(spec.url)
        if hints is not None:
            return StreamRow(
                name=name,
                url_type="hikvision",
                hikvision_structured=False,
                url_custom=spec.url,
                hv_user=hints.user,
                hv_password=hints.password_expr,
                hv_host=hints.host_expr,
                hv_port=str(hints.port),
                hv_channel=self._hints_channel_field(hints),
            )
        return StreamRow(
            name=name,
            url_type="hikvision",
            hikvision_structured=False,
            url_custom=spec.url,
        )

    def _refresh_list_widget(self) -> None:
        self._list.clear()
        for r in self._rows:
            self._list.addItem(QListWidgetItem(r.name or "(unnamed)"))

    def _current_row_index(self) -> int:
        return self._list.currentRow()

    def _apply_editor_panels_for_row(self, r: StreamRow) -> None:
        """Custom mode: URL panel only. Hikvision: always show fields card + optional full-URL row."""
        if r.url_type == "custom":
            self._hik_card.setVisible(False)
            self._url_panel.setVisible(True)
            self._url_panel_title.setText("Custom RTSP / URL")
            return
        self._hik_card.setVisible(True)
        self._url_panel.setVisible(False)
        opaque = not r.hikvision_structured
        self._hik_raw_block.setVisible(opaque)

    def _sync_ui_to_row(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._rows):
            return
        r = self._rows[idx]
        r.name = self._name_edit.text().strip()
        r.url_type = "hikvision" if self._mode_hik.isChecked() else "custom"
        if r.url_type == "custom":
            r.hikvision_structured = False
            r.url_custom = self._custom_url.text().strip()
        elif r.url_type == "hikvision" and not r.hikvision_structured:
            self._sync_opaque_rtsp_fields_to_raw_line(row_idx=idx)
            r.hv_user = self._hv_user.text().strip() or "admin"
            r.hv_password = self._hv_password.text()
            r.hv_host = self._hv_host.text().strip()
            r.hv_port = self._hv_port.text().strip() or "554"
            r.hv_channel = self._hv_channel.text().strip() or "101"
        else:
            r.hikvision_structured = True
            r.hv_user = self._hv_user.text().strip() or "admin"
            r.hv_password = self._hv_password.text()
            r.hv_host = self._hv_host.text().strip()
            r.hv_port = self._hv_port.text().strip() or "554"
            r.hv_channel = self._hv_channel.text().strip() or "101"

    def _load_row_into_ui(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._rows):
            return
        # Fill URL fields before radios so any stray signals see correct text;
        # block mode handler during reload/list switches.
        was_loading = self._loading_ui
        self._loading_ui = True
        try:
            r = self._rows[idx]
            self._name_edit.setText(r.name)
            self._hv_user.setText(r.hv_user)
            self._hv_password.setText(r.hv_password)
            self._hv_host.setText(r.hv_host)
            self._hv_port.setText(r.hv_port if r.hv_port else "554")
            self._hv_channel.setText(r.hv_channel if r.hv_channel else "101")
            if not r.hikvision_structured and r.url_type == "hikvision":
                self._hik_raw_url.setText(r.url_custom)
            else:
                self._hik_raw_url.clear()
            self._custom_url.setText(r.url_custom)
            self._mode_hik.setChecked(r.url_type == "hikvision")
            self._mode_custom.setChecked(r.url_type == "custom")
            self._apply_editor_panels_for_row(r)
            self._update_hik_preview()
        finally:
            self._loading_ui = was_loading

    @staticmethod
    def _hints_channel_field(hints: RtspHikEndpointHints) -> str:
        return hints.channel_suffix if hints.channel_suffix is not None else "101"

    def _apply_rtsp_hints_to_hv_widgets(self, hints: RtspHikEndpointHints) -> None:
        self._hv_user.setText(hints.user)
        self._hv_password.setText(hints.password_expr)
        self._hv_host.setText(hints.host_expr)
        self._hv_port.setText(str(hints.port))
        self._hv_channel.setText(self._hints_channel_field(hints))

    def _row_effective_url(self, r: StreamRow) -> str:
        if r.url_type == "custom":
            return r.url_custom.strip()
        if r.hikvision_structured:
            return build_hikvision_rtsp_url(
                r.hv_user,
                r.hv_password,
                r.hv_host,
                port=r.hv_port,
                channel=r.hv_channel,
            )
        return r.url_custom.strip()

    def _sync_opaque_rtsp_fields_to_raw_line(self, row_idx: int | None = None) -> None:
        """opaque Hikvision: push user/host/port/channel fields into saved URL text.

        Pass ``row_idx`` when syncing a stream that is **not** the list's current row
        (e.g. before switching streams: widgets still show that row).
        """
        if (
            self._loading_ui
            or not self._mode_hik.isChecked()
        ):
            return
        idx = (
            row_idx if row_idx is not None else self._current_row_index()
        )
        if idx < 0 or idx >= len(self._rows):
            return
        r = self._rows[idx]
        if r.url_type != "hikvision" or r.hikvision_structured:
            return
        base = self._hik_raw_url.text().strip() or r.url_custom.strip()
        user = self._hv_user.text().strip() or "admin"
        password = self._hv_password.text()
        host = self._hv_host.text().strip()
        port = self._hv_port.text().strip() or "554"
        ch = self._hv_channel.text().strip() or "101"
        if base:
            merged = merge_rtsp_netloc_into_url(base, user, password, host, port)
            merged = merge_channel_segment_in_hik_path(merged, ch)
        else:
            merged = build_hikvision_rtsp_url(
                user, password, host, port=port, channel=ch
            )
        was = self._loading_ui
        self._loading_ui = True
        try:
            self._hik_raw_url.setText(merged)
            r.url_custom = merged
        finally:
            self._loading_ui = was

    def _update_hik_preview(self) -> None:
        if not self._mode_hik.isChecked():
            return
        # Do not rely on *_hik_card / _hik_raw_block *.isVisible() — Qt returns False
        # for widgets on a non-current tab or before layout, which hid the preview
        # until a field emitted textChanged.
        if not self._rows:
            self._url_preview.setText("")
            return
        idx = self._current_row_index()
        if 0 <= idx < len(self._rows):
            r = self._rows[idx]
            if r.url_type == "hikvision" and not r.hikvision_structured:
                url = (
                    self._hik_raw_url.text().strip()
                    or (r.url_custom or "").strip()
                )
                self._url_preview.setText(url)
                return
        user = self._hv_user.text().strip() or "admin"
        password = self._hv_password.text()
        host = self._hv_host.text().strip()
        port = self._hv_port.text().strip() or "554"
        ch = self._hv_channel.text().strip() or "101"
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
                if r.url_type == "hikvision" and not r.hikvision_structured:
                    r.url_custom = self._hik_raw_url.text().strip()
                elif r.url_type == "hikvision" and r.hikvision_structured:
                    user = self._hv_user.text().strip() or "admin"
                    password = self._hv_password.text()
                    host = self._hv_host.text().strip()
                    port = self._hv_port.text().strip() or "554"
                    ch = self._hv_channel.text().strip() or "101"
                    r.url_custom = build_hikvision_rtsp_url(
                        user, password, host, port=port, channel=ch
                    )
                else:
                    r.url_custom = self._custom_url.text().strip()
                r.url_type = "custom"
                r.hikvision_structured = False
                self._custom_url.setText(r.url_custom)
                self._apply_editor_panels_for_row(r)
            else:
                r.url_type = "hikvision"
                src = self._custom_url.text().strip()
                parts = try_parse_hikvision_rtsp_url(src)
                if parts is not None:
                    r.hikvision_structured = True
                    r.hv_user = parts.user
                    r.hv_password = parts.password_expr
                    r.hv_host = parts.host_expr
                    r.hv_port = str(parts.port)
                    r.hv_channel = str(parts.channel)
                    self._hv_user.setText(r.hv_user)
                    self._hv_password.setText(r.hv_password)
                    self._hv_host.setText(r.hv_host)
                    self._hv_port.setText(r.hv_port)
                    self._hv_channel.setText(r.hv_channel)
                    self._hik_raw_url.clear()
                else:
                    r.hikvision_structured = False
                    r.url_custom = src
                    self._hik_raw_url.setText(src)
                    hints = extract_rtsp_hik_endpoint_hints(src)
                    if hints is not None:
                        self._apply_rtsp_hints_to_hv_widgets(hints)
                self._apply_editor_panels_for_row(r)
                self._update_hik_preview()
        finally:
            self._loading_ui = False

    def _on_hik_field_changed(self, *_args: object) -> None:
        if self._loading_ui:
            return
        idx = self._current_row_index()
        if (
            self._mode_hik.isChecked()
            and 0 <= idx < len(self._rows)
            and self._rows[idx].url_type == "hikvision"
            and not self._rows[idx].hikvision_structured
        ):
            self._sync_opaque_rtsp_fields_to_raw_line()
            self._url_preview.setText(self._hik_raw_url.text().strip())
            return
        self._update_hik_preview()

    def _on_custom_url_changed(self, *_args: object) -> None:
        if self._loading_ui:
            return
        idx = self._current_row_index()
        if idx >= 0 and self._mode_custom.isChecked():
            self._rows[idx].url_custom = self._custom_url.text()

    def _on_hik_raw_url_changed(self, *_args: object) -> None:
        if self._loading_ui:
            return
        idx = self._current_row_index()
        if (
            idx >= 0
            and self._mode_hik.isChecked()
            and self._rows[idx].url_type == "hikvision"
            and not self._rows[idx].hikvision_structured
        ):
            self._rows[idx].url_custom = self._hik_raw_url.text()
            self._update_hik_preview()

    def _on_hik_raw_url_editing_finished(self) -> None:
        if self._loading_ui:
            return
        idx = self._current_row_index()
        if idx < 0 or not self._mode_hik.isChecked():
            return
        r = self._rows[idx]
        if r.url_type != "hikvision" or r.hikvision_structured:
            return
        t = self._hik_raw_url.text().strip()
        hints = extract_rtsp_hik_endpoint_hints(t)
        if hints is None:
            return
        self._apply_rtsp_hints_to_hv_widgets(hints)

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
        LOG.info("Added stream row: %s_%d", base, n)
        self._refresh_list_widget()
        self._list.setCurrentRow(len(self._rows) - 1)

    def _remove_stream(self) -> None:
        idx = self._current_row_index()
        if idx < 0 or not self._rows:
            return
        removed_name = self._rows[idx].name
        del self._rows[idx]
        LOG.info("Removed stream row: %s", removed_name)
        if not self._rows:
            self._rows.append(StreamRow(name="camera_1"))
        self._refresh_list_widget()
        new_i = min(idx, len(self._rows) - 1)
        self._prev_list_row = -1
        self._list.setCurrentRow(new_i)

    def _collect_stream_specs(self) -> dict[str, StreamYamlSpec]:
        idx = self._current_row_index()
        if idx >= 0:
            self._sync_ui_to_row(idx)
        out: dict[str, StreamYamlSpec] = {}
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
            out[name] = StreamYamlSpec(url=url, url_type=r.url_type)
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
            stream_specs = self._collect_stream_specs()
        except ValueError as e:
            QMessageBox.warning(self, "Configuration", str(e))
            return

        data = load_config_document(self._config_path)
        data["streams"] = streams_to_yaml_entries(stream_specs)
        viewer = self._viewer_dict_from_ui(set(stream_specs.keys()))
        data["viewer"] = viewer
        self._viewer_changed = viewer != (self._initial_viewer_yaml or {})

        try:
            save_config_document(self._config_path, data)
        except OSError as e:
            QMessageBox.warning(self, "Configuration", f"Could not save YAML: {e}")
            return
        LOG.info(
            "Saved configuration via editor: %s (%d streams)",
            self._config_path,
            len(stream_specs),
        )

        if not self._env_edit.isEnabled():
            pass
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
            enc_path = self._default_new_env_enc_path()
            try:
                enc_path.parent.mkdir(parents=True, exist_ok=True)
                encrypt_plaintext_to_path(self._env_edit.toPlainText(), enc_path)
            except (OSError, RuntimeError, KeyringError) as e:
                QMessageBox.warning(
                    self, "Environment", f"Could not save .env.enc: {e}"
                )
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
    LOG.info("Opening config editor for: %s", config_path)
    dlg = ConfigEditorDialog(config_path, parent=parent)
    dlg.exec()
    LOG.info(
        "Config editor closed: saved=%s viewer_changed=%s",
        dlg.saved(),
        dlg.viewer_changed(),
    )
    return dlg.saved(), dlg.viewer_changed()
