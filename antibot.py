"""antibot.py — Deteccion y configuracion de sistemas anti-bot.

Ticketmaster usa:
- Queue-it (cola virtual)
- Imperva/Incapsula
- PerimeterX
- reCAPTCHA (v2/v3/Enterprise)
- Shape Security (protector de PAN)
"""
import config
import json_atomic


def _path():
    return config.ruta("antibot.json")


def get_antibot_config():
    default = {
        "queue_it": {
            "enabled": True,
            "detect_selectors": [".queue-it-wrapper", "#QueueItContent"],
            "wait_strategy": "human",
            "max_wait_minutes": 60,
        },
        "imperva": {"enabled": True, "detect_cookie": "incap_ses_"},
        "perimeterx": {"enabled": True, "detect_cookie": "_px"},
        "recaptcha": {
            "enabled": True,
            "mode": "auto",  # auto | semi | external
            "service": "2captcha",
            "api_key": "",
        },
        "shape_security": {"enabled": True},
        "stealth": {
            "enabled": True,
            "hide_webdriver": True,
            "canvas_noise": True,
            "webgl_spoof": True,
            "tls_fingerprint": True,
        },
    }
    return json_atomic.load(_path(), default=default)


def save_antibot_config(cfg):
    current = get_antibot_config()
    current.update(cfg)
    json_atomic.save(_path(), current)
    return current


def get_antibot_stats():
    return {
        "captchas_solved":   0,
        "queues_passed":     0,
        "blocks_bypassed":   0,
        "fingerprints_used": 0,
    }


def generate_fingerprint(domain_id=None):
    import random
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    ]
    return {
        "user_agent":       random.choice(uas),
        "timezone":         random.choice(["Europe/Madrid", "America/New_York", "Europe/London", "Europe/Berlin"]),
        "locale":           "en-US",
        "platform":         random.choice(["Win32", "MacIntel"]),
        "canvas_hash":      hex(random.randint(0, 2**32))[2:],
        "webgl_vendor":     "Google Inc. (NVIDIA)",
        "screen_resolution": random.choice(["1920x1080", "1366x768", "1440x900"]),
    }


class _CaptchaSolver:
    def solve(self, captcha_type, site_key=None, page_url=None):
        return {"ok": False, "error": "not_configured"}


captcha_solver = _CaptchaSolver()
