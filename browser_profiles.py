"""browser_profiles.py — Gestion de navegadores anti-deteccion."""
import config
import json_atomic


ANTIDETECT_BROWSERS = [
    {"id": "morelogin",  "name": "MoreLogin",     "url": "https://www.morelogin.com",   "price": "Freemium"},
    {"id": "gologin",    "name": "GoLogin",       "url": "https://gologin.com",         "price": "Paid"},
    {"id": "adspower",   "name": "AdsPower",      "url": "https://www.adspower.com",    "price": "Freemium"},
    {"id": "multilogin", "name": "Multilogin",    "url": "https://multilogin.com",      "price": "Paid"},
    {"id": "dolphin",    "name": "Dolphin Anty",  "url": "https://anty.dolphin.tech",   "price": "Freemium"},
    {"id": "incogniton", "name": "Incogniton",    "url": "https://incogniton.com",      "price": "Freemium"},
    {"id": "camoufox",   "name": "Camoufox",      "url": "https://camoufox.com",        "price": "Free"},
    {"id": "firefox",    "name": "Firefox (vanilla)", "url": "https://firefox.com",     "price": "Free"},
]


def get_antidetect_browsers():
    return ANTIDETECT_BROWSERS


def detect_installed_browsers():
    import shutil
    results = []
    candidates = [
        ("chrome",  "chrome.exe"),
        ("firefox", "firefox.exe"),
        ("edge",    "msedge.exe"),
        ("brave",   "brave.exe"),
    ]
    for name, exe in candidates:
        found = shutil.which(exe) is not None
        results.append({"id": name, "name": name.title(), "installed": found})
    return results


def get_extensions():
    d = json_atomic.load(config.ruta("extensions.json"), default={"extensions": []})
    return d.get("extensions", [])


def save_extensions(exts):
    json_atomic.save(config.ruta("extensions.json"), {"extensions": exts})
