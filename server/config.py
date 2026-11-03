"""Configuration loaded from environment variables (and a `.env`
file if present).

Keep this module dependency-free so it can be imported anywhere (including
tests) without risk of import side effects.
"""

from __future__ import annotations

import codecs
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` pairs from ``path`` into ``os.environ``.

    Stdlib-only replacement for ``python-dotenv``. Existing
    environment variables always win (no override). Blank lines,
    comments (``#``), and lines without ``=`` are skipped; values
    may be wrapped in single or double quotes.

    Encoding is handled defensively because ``.env`` files are
    hand-edited in whatever the editor defaults to: a UTF-16
    (Notepad "Unicode") save is detected via its BOM and decoded
    correctly, a UTF-8 BOM is stripped, and undecodable bytes are
    replaced rather than crashing startup.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(
        codecs.BOM_UTF16_BE,
    ):
        raw = data.decode("utf-16")
    else:
        # utf-8-sig strips a leading UTF-8 BOM if present.
        raw = data.decode("utf-8-sig", errors="replace")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value


#: Settings used to be named ``LR_*``; those names are still read.
LEGACY_PREFIX = "LR_"
PREFIX = "AFTERWORD_"
#: Old names that map onto a different setting, in priority order: the
#: OpenRouter key used to serve both models, the MiniMax key the text one.
_LEGACY_ALIASES: dict[str, tuple[str, ...]] = {
    "LR_MINIMAX_API_KEY": ("AFTERWORD_TEXT_API_KEY",),
    "LR_OPENROUTER_API_KEY": (
        "AFTERWORD_TRANSCRIPTION_API_KEY",
        "AFTERWORD_TEXT_API_KEY",
    ),
}


def apply_legacy_names(environ=os.environ) -> None:
    """Copy settings still named ``LR_*`` onto their ``AFTERWORD_*`` names.

    A value already set under the new name always wins, so an old
    ``.env`` keeps working and can be renamed at any time.
    """

    def copy(old: str, new: str) -> None:
        value = environ.get(old, "")
        if value != "" and environ.get(new, "") == "":
            environ[new] = value

    for old in [k for k in environ if k.startswith(LEGACY_PREFIX)]:
        if old not in _LEGACY_ALIASES:
            copy(old, PREFIX + old[len(LEGACY_PREFIX) :])
    for old, targets in _LEGACY_ALIASES.items():
        for new in targets:
            copy(old, new)


def _env(key: str, *fallback_keys: str, default: str = "") -> str:
    """First non-empty value among ``key`` and ``fallback_keys``."""
    for k in (key, *fallback_keys):
        value = os.environ.get(k)
        if value is not None and value != "":
            return value
    return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_dir(raw: str, base: Path) -> Path:
    """``raw`` as an absolute path; relative paths are taken from
    ``base`` (the server folder), not the current directory, so
    starting ``app.py`` from elsewhere cannot create a second,
    empty data dir."""
    p = Path(raw).expanduser()
    return (p if p.is_absolute() else base / p).resolve()


@dataclass(frozen=True)
class Config:
    """Immutable configuration snapshot.

    Construct via :func:`load` so defaults are applied consistently.
    """

    data_dir: Path
    host: str
    port: int
    transcription_api_key: str
    text_api_key: str
    ffmpeg_path: str
    max_upload_bytes: int
    # Access control (see auth.py). Empty = off, for local development.
    device_token: str = ""  # devices send "Authorization: Bearer <token>"
    web_password: str = ""  # one password for the web UI
    secret_key: str = ""  # session cookie key; empty = generated + kept
    cookie_secure: bool = False  # True behind HTTPS (Caddy)
    allow_open: bool = False  # serve beyond this PC without a password

    # Convenience paths derived from data_dir.
    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "metadata.db"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def inbox_dir(self) -> Path:
        """Temp dir for in-flight web uploads. Cleaned on startup."""
        return self.data_dir / "inbox"


def load() -> Config:
    """Build a Config from the current process environment.

    A ``.env`` file is loaded first (CWD, then the directory
    containing this module) without overriding real env vars, then
    old ``LR_*`` names are copied onto their new names
    (:func:`apply_legacy_names`).
    """
    here = Path(__file__).resolve().parent
    for candidate in (Path.cwd() / ".env", here / ".env"):
        _load_dotenv(candidate)
    apply_legacy_names()
    return Config(
        data_dir=_resolve_dir(_env("AFTERWORD_DATA_DIR", default="data"), here),
        host=_env("AFTERWORD_HOST", default="127.0.0.1"),
        port=_env_int("AFTERWORD_PORT", 8000),
        # Both providers go through OpenRouter; the same key works
        # for both.
        transcription_api_key=_env("AFTERWORD_TRANSCRIPTION_API_KEY"),
        text_api_key=_env("AFTERWORD_TEXT_API_KEY"),
        ffmpeg_path=_env("AFTERWORD_FFMPEG_PATH", default="ffmpeg"),
        max_upload_bytes=_env_int("AFTERWORD_MAX_UPLOAD_BYTES", 500 * 1024 * 1024),
        device_token=_env("AFTERWORD_DEVICE_TOKEN"),
        web_password=_env("AFTERWORD_WEB_PASSWORD"),
        secret_key=_env("AFTERWORD_SECRET"),
        cookie_secure=_flag("AFTERWORD_COOKIE_SECURE"),
        allow_open=_flag("AFTERWORD_ALLOW_OPEN"),
    )


def _flag(key: str) -> bool:
    return _env(key).strip().lower() in ("1", "true", "yes", "on")


def is_loopback(host: str) -> bool:
    """True when ``host`` only accepts connections from this machine."""
    host = host.strip().strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def exposure_problem(cfg: Config) -> str:
    """Why the server must not start with ``cfg``, or "" when it may.

    Listening beyond this machine (e.g. ``0.0.0.0``) without a web
    password would leave every recording open to the network, so it
    needs ``AFTERWORD_WEB_PASSWORD``, or ``AFTERWORD_ALLOW_OPEN=1`` as a
    deliberate choice on a trusted network.
    """
    if is_loopback(cfg.host) or cfg.web_password or cfg.allow_open:
        return ""
    return (
        f"AFTERWORD_HOST={cfg.host} makes the server reachable from other "
        "devices, but AFTERWORD_WEB_PASSWORD is not set. Set a password, "
        "use AFTERWORD_HOST=127.0.0.1, or set AFTERWORD_ALLOW_OPEN=1 to "
        "run without a password on a network you trust."
    )
