"""settings_manager.py — Configuracion global del dashboard."""
import config
import json_atomic


DEFAULT_SETTINGS = {
    "language":            "es",
    "theme":               "dark",
    "monitor_interval":    30,
    "max_threads":         10,
    "headless":            False,
    "auto_start":          False,
    "primary_color":       "#026CDF",  # TM Azure
    "notifications_enabled": True,
    "sse_enabled":         True,
    "dashboard_port":      5055,
    "countdown_autostart": False,
    "debug_mode":          False,
}


def _path():
    return config.ruta("settings.json")


def get_settings():
    d = json_atomic.load(_path(), default=DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(d)
    return merged


def save_settings(patch):
    current = get_settings()
    current.update(patch)
    json_atomic.save(_path(), current)
    return current
