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


def expand_env(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        v = os.environ.get(name)
        if v is None:
            raise KeyError(f"Environment variable not set: {name}")
        return v

    return _PLACEHOLDER.sub(repl, value)


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
