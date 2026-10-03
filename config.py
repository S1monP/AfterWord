"""Configuration loaded from environment variables.

Keep this module dependency-free so it can be imported anywhere (including
tests) without risk of import side effects.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value is not None and value != "" else default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Immutable configuration snapshot.

    Construct via :func:`load` so defaults are applied consistently.
    """
    data_dir: Path
    host: str
    port: int
    openrouter_api_key: str
    minimax_api_key: str
    ffmpeg_path: str
    max_upload_bytes: int

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
    """Build a Config from the current process environment."""
    return Config(
        data_dir=Path(_env("LR_DATA_DIR", "./data")).resolve(),
        host=_env("LR_HOST", "0.0.0.0"),
        port=_env_int("LR_PORT", 8000),
        openrouter_api_key=_env("LR_OPENROUTER_API_KEY", ""),
        minimax_api_key=_env("LR_MINIMAX_API_KEY", ""),
        ffmpeg_path=_env("LR_FFMPEG_PATH", "ffmpeg"),
        max_upload_bytes=_env_int("LR_MAX_UPLOAD_BYTES", 500 * 1024 * 1024),
    )
