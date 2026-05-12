import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
import logging
from typing import Literal

import yaml
from dotenv import load_dotenv

from hikvision_viewer.env_secure import decrypt_env_file_to_str

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
LOG = logging.getLogger(__name__)

StreamUrlType = Literal["hikvision", "custom"]
DEFAULT_STREAM_URL_TYPE: StreamUrlType = "hikvision"
_ALLOWED_STREAM_URL_TYPES = frozenset(("hikvision", "custom"))


@dataclass(frozen=True)
class StreamYamlSpec:
    """One stream entry as stored under `streams:` in YAML (before env expansion)."""

    url: str
    url_type: StreamUrlType = DEFAULT_STREAM_URL_TYPE


def normalize_stream_url_type(raw: object | None) -> StreamUrlType:
    """Return a valid url_type; default is hikvision when missing."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_STREAM_URL_TYPE
    if not isinstance(raw, str):
        raise ValueError(f"stream url_type must be a string, got {type(raw).__name__}")
    s = raw.strip().lower()
    if s not in _ALLOWED_STREAM_URL_TYPES:
        raise ValueError(
            "stream url_type must be 'hikvision' or 'custom', "
            f"got {raw!r}"
        )
    return s  # type: ignore[return-value]


def infer_legacy_stream_url_type(url: str) -> StreamUrlType:
    """If `url_type` is omitted in YAML, match pre–url_type behavior and NVR placeholders.

    Fully-resolved Hikvision paths are detected via :func:`try_parse_hikvision_rtsp_url`.
    URLs with ``{ENV}`` placeholders in the channel segment are still treated as
    **hikvision** when the path looks like ``/Streaming/Channels/<anything>``.
    """
    from urllib.parse import urlparse

    from hikvision_viewer.hikvision_rtsp import try_parse_hikvision_rtsp_url

    if try_parse_hikvision_rtsp_url(url) is not None:
        return "hikvision"
    u = urlparse(url.strip())
    if u.scheme != "rtsp":
        return "custom"
    path = (u.path or "").replace("\\", "/").rstrip("/")
    if re.search(r"/Streaming/Channels/.+", path):
        return "hikvision"
    return "custom"


def parse_stream_entry(name: str, spec: object) -> StreamYamlSpec:
    """Normalize a YAML `streams:` value to url + url_type."""
    if isinstance(spec, str):
        url = spec
        ut = infer_legacy_stream_url_type(url)
        return StreamYamlSpec(url=url, url_type=ut)
    if isinstance(spec, dict):
        raw_url = spec.get("url")
        if not isinstance(raw_url, str):
            raise ValueError(f"stream {name!r}: dict entry must contain a string 'url'")
        if "url_type" in spec and spec.get("url_type") is not None:
            try:
                ut = normalize_stream_url_type(spec.get("url_type"))
            except ValueError as e:
                raise ValueError(f"stream {name!r}: {e}") from e
        else:
            ut = infer_legacy_stream_url_type(raw_url)
        return StreamYamlSpec(url=raw_url, url_type=ut)
    raise ValueError(f"stream {name!r}: expected string or {{url: ...}}")


def app_config_dir() -> Path:
    """XDG config directory: $XDG_CONFIG_HOME/hikvision-viewer (default ~/.config/hikvision-viewer)."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser().resolve() if xdg else Path.home() / ".config"
    return base / "hikvision-viewer"


def resolve_config_path() -> Path:
    """Prefer XDG config; fall back to a config file next to the package (dev checkout)."""
    d = app_config_dir()
    for name in ("config.yaml", "config.yml"):
        p = d / name
        if p.is_file():
            return p
    pkg = Path(__file__).resolve().parent
    for name in ("config.yaml", "config.yml"):
        p = pkg / name
        if p.is_file():
            return p
    # checkout layout: config next to the hikvision_viewer/ package directory
    for name in ("config.yaml", "config.yml"):
        p = pkg.parent / name
        if p.is_file():
            return p
    return d / "config.yaml"


def _load_dotenv_dir(base: Path, *, override: bool) -> None:
    enc = base / ".env.enc"
    if enc.is_file():
        LOG.info("Loading encrypted environment file: %s", enc)
        try:
            text = decrypt_env_file_to_str(enc)
        except Exception as e:
            LOG.exception("Failed decrypting env file: %s", enc)
            raise ValueError(f"Cannot decrypt {enc}: {e}") from e
        load_dotenv(stream=io.StringIO(text), override=override)


def _apply_dotenv(config_path: Path) -> None:
    """Load `.env.enc` from the app XDG dir, then from the config directory (override)."""
    _load_dotenv_dir(app_config_dir(), override=False)
    _load_dotenv_dir(config_path.parent, override=True)


def _environ_casefold_index() -> dict[str, str]:
    """First value per case-folded name (POSIX env keys are case-sensitive)."""
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        ck = k.casefold()
        if ck not in out:
            out[ck] = v
    return out


def expand_env(value: str) -> str:
    """Replace {VAR} from the environment; exact key match first, then case-insensitive."""

    ci = _environ_casefold_index()

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        v = os.environ.get(name)
        if v is None:
            v = ci.get(name.casefold())
        if v is None:
            raise KeyError(f"Environment variable not set: {name}")
        return v

    return _PLACEHOLDER.sub(repl, value)


def ordered_stream_names(config_path: Path, streams: dict[str, str]) -> list[str]:
    """Stream names for tile/stack order: viewer.single_view_order first, then sorted remainder."""
    names = set(streams.keys())
    if not names:
        return []
    raw_order: list[str] = []
    if config_path.is_file():
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            viewer = data.get("viewer")
            if isinstance(viewer, dict):
                order = viewer.get("single_view_order")
                if isinstance(order, list):
                    raw_order = [str(x) for x in order if isinstance(x, str) and x.strip()]
        except (OSError, yaml.YAMLError):
            raw_order = []
    out: list[str] = []
    seen: set[str] = set()
    for n in raw_order:
        if n in names and n not in seen:
            out.append(n)
            seen.add(n)
    for n in sorted(names - seen):
        out.append(n)
    return out


def load_streams(config_path: Path) -> dict[str, str]:
    LOG.info("Loading streams from config: %s", config_path)
    _apply_dotenv(config_path)
    data = yaml.safe_load(config_path.read_text())
    if not data or "streams" not in data:
        raise ValueError("config must contain a 'streams' mapping")
    out: dict[str, str] = {}
    streams = data["streams"]
    if not isinstance(streams, dict):
        raise ValueError("'streams' must be a mapping")
    for name, spec in streams.items():
        entry = parse_stream_entry(str(name), spec)
        out[str(name)] = expand_env(entry.url)
    LOG.info("Loaded %d stream definitions", len(out))
    return out


def load_config_document(config_path: Path) -> dict:
    """YAML document as dict for editing; does not load dotenv or expand placeholders."""
    if not config_path.is_file():
        return {}
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def save_config_document(config_path: Path, data: dict) -> None:
    """Write YAML atomically (same directory)."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(config_path)
    LOG.info("Saved configuration document: %s", config_path)


def _env_set_if_unset(name: str, value: str) -> None:
    cur = os.environ.get(name)
    if cur is not None and str(cur).strip():
        return
    os.environ[name] = value


def apply_viewer_from_yaml(config_path: Path) -> None:
    """Apply optional viewer: block to process env; existing non-empty env wins."""
    if not config_path.is_file():
        LOG.info("No config found for viewer defaults: %s", config_path)
        return
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    viewer = data.get("viewer")
    if not isinstance(viewer, dict):
        LOG.info("No viewer block present in config: %s", config_path)
        return
    LOG.info("Applying viewer defaults from config: %s", config_path)
    if "mpv_subprocess" in viewer and isinstance(viewer["mpv_subprocess"], bool):
        _env_set_if_unset(
            "HIKVISION_MPV_SUBPROCESS", "1" if viewer["mpv_subprocess"] else "0"
        )
    if "mpv_hwdec" in viewer and isinstance(viewer["mpv_hwdec"], str):
        s = viewer["mpv_hwdec"].strip()
        if s:
            _env_set_if_unset("HIKVISION_MPV_HWDEC", s)
    if "mpv_vo" in viewer and isinstance(viewer["mpv_vo"], str):
        s = viewer["mpv_vo"].strip()
        if s:
            _env_set_if_unset("HIKVISION_MPV_VO", s)
    if "qt_wayland" in viewer and isinstance(viewer["qt_wayland"], bool):
        _env_set_if_unset(
            "HIKVISION_QT_WAYLAND", "1" if viewer["qt_wayland"] else "0"
        )
    if "force_dark_mode" in viewer and isinstance(viewer["force_dark_mode"], bool):
        _env_set_if_unset(
            "HIKVISION_FORCE_DARK", "1" if viewer["force_dark_mode"] else "0"
        )


def parse_streams_raw(data: dict) -> dict[str, StreamYamlSpec]:
    """Extract stream name -> URL + url_type without expanding env."""
    if not data or "streams" not in data:
        return {}
    streams = data["streams"]
    if not isinstance(streams, dict):
        return {}
    out: dict[str, StreamYamlSpec] = {}
    for name, spec in streams.items():
        out[str(name)] = parse_stream_entry(str(name), spec)
    return out


def streams_to_yaml_entries(specs: dict[str, StreamYamlSpec]) -> dict[str, dict[str, str]]:
    """Normalize to name -> {url, url_type} for YAML under streams:."""
    return {
        k: {"url": v.url, "url_type": v.url_type} for k, v in specs.items()
    }
