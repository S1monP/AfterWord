"""The web UI's settings: theme mode and colour scheme. Stored by the
server, so every device shows the same, and sent with every page
(``ui_settings``), so a page is drawn in the right scheme at once."""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from web.common import current_app_storage

bp = Blueprint("settings_api", __name__)

# Allowed values per setting; the first is the default.
CHOICES = {
    "theme": ("auto", "light", "dark"),
    "scheme": ("violet", "honey", "wine", "copper"),
}


def ui_settings() -> dict[str, str]:
    """The current settings, with defaults for anything not stored."""
    stored = current_app_storage().get_settings()
    return {
        key: stored.get(key) if stored.get(key) in allowed else allowed[0]
        for key, allowed in CHOICES.items()
    }


@bp.get("/api/v1/settings")
def api_get_settings() -> Any:
    return jsonify({"settings": ui_settings()})


@bp.patch("/api/v1/settings")
def api_update_settings() -> Any:
    """Body: any of ``{"theme": ..., "scheme": ...}``."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_json"}), 400
    for key, value in payload.items():
        if key not in CHOICES or value not in CHOICES[key]:
            return jsonify({"error": "invalid_setting", "key": key}), 400
    store = current_app_storage()
    for key, value in payload.items():
        store.set_setting(key, value)
    return jsonify({"settings": ui_settings()})
