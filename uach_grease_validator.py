"""uach_grease_validator.py — UA-CH GREASE consistency validator.

Chrome 2026 reduce minor versions a 0.0.0 y ships GREASE entries en Sec-CH-UA.
Bots con versions reales en UA pero Sec-CH-UA-* válidos AHORA destacan.

Solución: validar y construir UA + Sec-CH-UA-* coherentes con GREASE.

API:
  build_chrome_headers(major_version=131) → dict completo {UA, Sec-CH-UA-*}
  build_firefox_headers(major_version=128)
  validate_uach_consistency(headers) → ok/issues
  rotate_grease_entry()
  self_test()
"""
import random
from typing import Dict, List

# GREASE entries reales que Chrome usa (RFC 8701)
GREASE_VENDORS = [
    "Not;A Brand", "Not(A:Brand)", "Not?A_Brand", "Not.A/Brand",
    "Not_A;Brand", "Not.A;Brand",
]

# Versiones GREASE típicas de Chrome
GREASE_VERSIONS = ["8", "24", "99"]


def _pick_grease() -> Dict:
    return {
        "name": random.choice(GREASE_VENDORS),
        "version": random.choice(GREASE_VERSIONS),
    }


def build_chrome_headers(major_version: int = 131,
                          platform: str = "Windows",
                          architecture: str = "x86",
                          bitness: str = "64",
                          mobile: bool = False) -> Dict[str, str]:
    """Headers Chrome reales con UA reducido (0.0.0) + Sec-CH-UA-* coherentes."""
    grease = _pick_grease()
    chromium = {"name": "Chromium", "version": str(major_version)}
    real = {"name": "Google Chrome", "version": str(major_version)}
    # Brands (orden alfabético-pseudo random Chromium-style)
    brands = [grease, chromium, real]
    random.shuffle(brands)
    sec_ch_ua = ", ".join(f'"{b["name"]}";v="{b["version"]}"' for b in brands)
    # Full versions list — usa 0.0.0.0 según privacy update
    sec_ch_ua_full = ", ".join(
        f'"{b["name"]}";v="{b["version"]}.0.0.0"' for b in brands
    )
    # UA reducido — minor version SIEMPRE 0.0.0
    if mobile:
        ua = (f"Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/{major_version}.0.0.0 Mobile Safari/537.36")
        sec_ch_ua_mobile = "?1"
        sec_ch_ua_platform = '"Android"'
    else:
        plat_lookup = {"Windows": "Windows NT 10.0", "macOS": "Macintosh; Intel Mac OS X 10_15_7",
                        "Linux": "X11; Linux x86_64"}
        ua = (f"Mozilla/5.0 ({plat_lookup.get(platform,'Windows NT 10.0')}) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/{major_version}.0.0.0 Safari/537.36")
        sec_ch_ua_mobile = "?0"
        sec_ch_ua_platform = f'"{platform}"'
    return {
        "User-Agent": ua,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": sec_ch_ua_mobile,
        "sec-ch-ua-platform": sec_ch_ua_platform,
        "sec-ch-ua-full-version-list": sec_ch_ua_full,
        "sec-ch-ua-arch": f'"{architecture}"',
        "sec-ch-ua-bitness": f'"{bitness}"',
        "sec-ch-ua-platform-version": '"15.0.0"' if platform == "Windows" else '""',
        "sec-ch-ua-model": '""',
    }


def build_firefox_headers(major_version: int = 128) -> Dict[str, str]:
    """Firefox NO envía Sec-CH-UA-* (no implementa Client Hints)."""
    return {
        "User-Agent": (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{major_version}.0) "
                        f"Gecko/20100101 Firefox/{major_version}.0"),
        # Firefox NO envía Sec-CH-UA — añadirlos sería detección obvia
    }


def validate_uach_consistency(headers: Dict[str, str]) -> Dict:
    """Verifica que headers UA + Sec-CH-UA-* sean coherentes."""
    issues = []
    ua = headers.get("User-Agent", "") or headers.get("user-agent", "")
    sec_ch_ua = headers.get("sec-ch-ua", "")
    sec_ch_ua_mobile = headers.get("sec-ch-ua-mobile", "")
    sec_ch_ua_platform = headers.get("sec-ch-ua-platform", "")

    if "Chrome/" in ua and not sec_ch_ua:
        issues.append("Chrome UA sin sec-ch-ua header")
    if "Firefox/" in ua and sec_ch_ua:
        issues.append("Firefox UA con sec-ch-ua (Firefox no lo envía)")
    if sec_ch_ua and not any(g in sec_ch_ua for g in GREASE_VENDORS):
        issues.append("sec-ch-ua sin GREASE entry (sospechoso para Chrome)")
    # Verificar minor=0
    import re as _re
    m = _re.search(r"Chrome/(\d+)\.(\d+)\.(\d+)\.(\d+)", ua)
    if m and (m.group(2) != "0" or m.group(3) != "0" or m.group(4) != "0"):
        issues.append(f"Chrome UA con minor != 0.0.0 (UA reduction roto): "
                       f"{m.group(0)}")
    # Mobile match
    is_mobile_ua = "Mobile" in ua or "Android" in ua
    is_mobile_hint = sec_ch_ua_mobile == "?1"
    if is_mobile_ua != is_mobile_hint and sec_ch_ua_mobile:
        issues.append(f"Mobile mismatch: UA={is_mobile_ua} hint={is_mobile_hint}")
    return {"ok": not issues, "issues": issues, "checked": len(headers)}


def rotate_grease_entry() -> Dict:
    """Devuelve un nuevo GREASE entry para ser inyectado en headers existentes."""
    g = _pick_grease()
    return {"brand": g["name"], "version": g["version"]}


def self_test() -> Dict:
    r = {"checks": {}}
    # Build chrome
    h = build_chrome_headers(131)
    r["checks"]["chrome_has_ua"] = "Chrome/131.0.0.0" in h["User-Agent"]
    r["checks"]["chrome_minor_zero"] = "131.0.0.0" in h["User-Agent"]  # NO minor real
    r["checks"]["chrome_has_sec_ch_ua"] = "sec-ch-ua" in h
    r["checks"]["chrome_has_grease"] = any(g in h["sec-ch-ua"] for g in GREASE_VENDORS)
    r["checks"]["chrome_mobile_zero"] = h["sec-ch-ua-mobile"] == "?0"

    # Mobile
    h_mob = build_chrome_headers(131, mobile=True)
    r["checks"]["mobile_ua_has_android"] = "Android" in h_mob["User-Agent"]
    r["checks"]["mobile_hint_one"] = h_mob["sec-ch-ua-mobile"] == "?1"
    r["checks"]["mobile_platform_android"] = '"Android"' == h_mob["sec-ch-ua-platform"]

    # Firefox sin sec-ch-ua
    h_ff = build_firefox_headers(128)
    r["checks"]["firefox_no_sec_ch_ua"] = "sec-ch-ua" not in h_ff
    r["checks"]["firefox_ua_has_gecko"] = "Gecko" in h_ff["User-Agent"]

    # Validate
    v_ok = validate_uach_consistency(h)
    r["checks"]["validate_chrome_ok"] = v_ok["ok"]

    # Validate mismatch — Chrome UA con minor real
    bad = {"User-Agent": "Mozilla/5.0 ... Chrome/131.0.6778.85 Safari/537.36",
           "sec-ch-ua": '"Chromium";v="131"'}
    v_bad = validate_uach_consistency(bad)
    r["checks"]["validate_detects_minor"] = not v_bad["ok"]

    # Validate Firefox con sec-ch-ua (anomalía)
    ff_bad = {"User-Agent": "Mozilla/5.0 Firefox/128.0",
              "sec-ch-ua": '"Chromium";v="128"'}
    v_ffbad = validate_uach_consistency(ff_bad)
    r["checks"]["validate_detects_ff_anomaly"] = not v_ffbad["ok"]

    # Rotate
    rg = rotate_grease_entry()
    r["checks"]["rotate_returns_grease"] = rg["brand"] in GREASE_VENDORS

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    return {"module": "uach_grease_validator", "version": 1,
            "grease_vendors": len(GREASE_VENDORS)}
