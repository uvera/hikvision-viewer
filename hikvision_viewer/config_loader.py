import io
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

from hikvision_viewer.env_secure import decrypt_env_file_to_str

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


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


def resolve_plain_dotenv_path(config_path: Path) -> Path | None:
    """First existing plaintext `.env`: next to config, then XDG app dir."""
    for base in (config_path.parent, app_config_dir()):
        p = base / ".env"
        if p.is_file():
            return p
    return None


def _load_dotenv_dir(base: Path, *, override: bool) -> None:
    plain = base / ".env"
    enc = base / ".env.enc"
    if plain.is_file():
        load_dotenv(plain, override=override)
    elif enc.is_file():
        try:
            text = decrypt_env_file_to_str(enc)
        except Exception as e:
            raise ValueError(f"Cannot decrypt {enc}: {e}") from e
        load_dotenv(stream=io.StringIO(text), override=override)


def _apply_dotenv(config_path: Path) -> None:
    """Load .env / .env.enc from the app XDG dir, then from the config directory (override)."""
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
    _apply_dotenv(config_path)
    data = yaml.safe_load(config_path.read_text())
    if not data or "streams" not in data:
        raise ValueError("config must contain a 'streams' mapping")
    out: dict[str, str] = {}
    for name, spec in data["streams"].items():
        if isinstance(spec, str):
            url = spec
        elif isinstance(spec, dict) and "url" in spec:
            url = spec["url"]
        else:
            raise ValueError(f"stream {name!r}: expected string or {{url: ...}}")
        if not isinstance(url, str):
            raise ValueError(f"stream {name!r}: url must be a string")
        out[str(name)] = expand_env(url)
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


def _env_set_if_unset(name: str, value: str) -> None:
    cur = os.environ.get(name)
    if cur is not None and str(cur).strip():
        return
    os.environ[name] = value


def apply_viewer_from_yaml(config_path: Path) -> None:
    """Apply optional viewer: block to process env; existing non-empty env wins."""
    if not config_path.is_file():
        return
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    viewer = data.get("viewer")
    if not isinstance(viewer, dict):
        return
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


def parse_streams_raw(data: dict) -> dict[str, str]:
    """Extract stream name -> url strings without expanding env."""
    if not data or "streams" not in data:
        return {}
    streams = data["streams"]
    if not isinstance(streams, dict):
        return {}
    out: dict[str, str] = {}
    for name, spec in streams.items():
        if isinstance(spec, str):
            out[str(name)] = spec
        elif isinstance(spec, dict) and isinstance(spec.get("url"), str):
            out[str(name)] = spec["url"]
    return out


def streams_to_yaml_entries(streams: dict[str, str]) -> dict:
    """Normalize to name -> {url: ...} for YAML under streams:."""
    return {k: {"url": v} for k, v in streams.items()}
