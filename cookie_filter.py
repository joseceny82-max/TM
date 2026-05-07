"""Clasificador de cookies — distingue útiles (KEEP) de malas (DROP) al cerrar un perfil.

Regla del usuario: al cerrar un perfil MoreLogin, NO eliminar todas las cookies — preservar las
que sirven al proyecto (sesión TM, antibot, Adyen, queue-it) y borrar solo las "malas" (tracking
de terceros, consent, analytics, etc.).

Usado por `_buy_close.py` y cualquier flujo de cleanup post-run.

API:
    classify_cookie(cookie) -> 'keep' | 'drop' | 'unknown'
    partition_cookies(cookies) -> (keep, drop, unknown)
"""
from __future__ import annotations
import re
from typing import Dict, List, Tuple, Iterable

# ───────────────────────────────────────────────────────────────────────
# KEEP — Útiles para la operación del proyecto. Borrar perdería estado valioso.
# ───────────────────────────────────────────────────────────────────────
KEEP_NAME_PATTERNS = [
    # TM session/auth
    re.compile(r'^TMUO$', re.I),
    re.compile(r'^tm[-_]?(session|sessId|sid|user|uid|auth|token)', re.I),
    re.compile(r'^eps[-_]?session', re.I),
    re.compile(r'^myacc', re.I),
    re.compile(r'^TMPS', re.I),
    re.compile(r'^reese84$', re.I),  # Reblaze antibot, conserva fingerprint válido
    re.compile(r'^SID', re.I),
    # Akamai / antibot (mantienen fingerprint validado)
    re.compile(r'^_abck$', re.I),
    re.compile(r'^bm_(sz|sv|mi|so|ni|s)$', re.I),
    re.compile(r'^ak_bmsc$', re.I),
    re.compile(r'^BIGipServer', re.I),
    # Queue-it
    re.compile(r'^Queue-?it', re.I),
    re.compile(r'^acpat', re.I),
    # Adyen / pago
    re.compile(r'^_adyen', re.I),
    re.compile(r'^adyenJS', re.I),
    # PerimeterX/Imperva (algunos eventos)
    re.compile(r'^_px3?$', re.I),
    re.compile(r'^_pxhd$', re.I),
    re.compile(r'^_px(?:Captcha)?$', re.I),
    re.compile(r'^visid_incap', re.I),
    re.compile(r'^incap_ses', re.I),
    # Cloudflare
    re.compile(r'^cf_clearance$', re.I),
    re.compile(r'^__cf_bm$', re.I),
]

# Hostnames cuyas cookies se conservan SIEMPRE (whitelisted), salvo nombres explícitamente DROP.
KEEP_DOMAIN_SUFFIXES = (
    '.ticketmaster.com', 'ticketmaster.com',
    '.ticketmaster.es', 'ticketmaster.es',
    '.ticketmaster.co.uk', 'ticketmaster.co.uk',
    '.ticketmaster.de', '.ticketmaster.fr', '.ticketmaster.it',
    '.ticketmaster.com.au', '.ticketmaster.ca', '.ticketmaster.com.mx',
    '.livenation.com', 'livenation.com',
    '.queue-it.net', 'queue-it.net',
    '.adyen.com', 'adyen.com',
    '.identity.ticketmaster.com', 'identity.ticketmaster.es',
    '.auth.ticketmaster.com',
)

# ───────────────────────────────────────────────────────────────────────
# DROP — Tracking, analytics, consent. No aportan al bot.
# ───────────────────────────────────────────────────────────────────────
DROP_NAME_PATTERNS = [
    # Google Analytics / Tag Manager / Ads
    re.compile(r'^_g(a|id|at)(_|$)', re.I),
    re.compile(r'^_ga$', re.I),
    re.compile(r'^_dc_gtm', re.I),
    re.compile(r'^_gcl_', re.I),
    re.compile(r'^__utm[a-z]?$', re.I),
    # Facebook
    re.compile(r'^_fb[pc]$', re.I),
    re.compile(r'^fr$', re.I),
    # Microsoft Clarity / Bing
    re.compile(r'^_clc?k|^_clsk', re.I),
    re.compile(r'^MUID', re.I),
    # Pinterest
    re.compile(r'^_pin_', re.I),
    # OneTrust consent
    re.compile(r'^Optanon', re.I),
    re.compile(r'^OTAdditionalConsentString', re.I),
    re.compile(r'^eupubconsent', re.I),
    re.compile(r'^OneTrust', re.I),
    # IAB consent
    re.compile(r'^euconsent', re.I),
    re.compile(r'^usprivacy', re.I),
    # HubSpot
    re.compile(r'^__hssc|^__hstc|^__hssrc|^hubspotutk', re.I),
    # Tiktok
    re.compile(r'^_tt_enable_cookie|^_ttp$', re.I),
    # AT Internet, Mixpanel, Segment, etc.
    re.compile(r'^mp_', re.I),
    re.compile(r'^ajs_', re.I),
    # Quantcast
    re.compile(r'^__qca$', re.I),
]

DROP_DOMAIN_SUFFIXES = (
    '.google.com', 'google.com',
    '.google-analytics.com', 'google-analytics.com',
    '.googletagmanager.com', 'googletagmanager.com',
    '.doubleclick.net', 'doubleclick.net',
    '.facebook.com', 'facebook.com',
    '.fbcdn.net',
    '.bing.com', 'bing.com',
    '.clarity.ms', 'clarity.ms',
    '.linkedin.com',
    '.pinterest.com',
    '.tiktok.com',
    '.hotjar.com', 'hotjar.com',
    '.onetrust.com', 'onetrust.com',
    '.cookielaw.org',
    '.optanon.com',
    '.quantcast.com',
)


def classify_cookie(c: Dict) -> str:
    """Devuelve 'keep' | 'drop' | 'unknown'.
    Regla:
      1) Si el nombre matchea DROP → drop (gana sobre dominio).
      2) Si el dominio matchea DROP_DOMAIN → drop.
      3) Si el nombre matchea KEEP → keep.
      4) Si el dominio matchea KEEP_DOMAIN → keep.
      5) Resto → unknown (por defecto se borra para mantener perfil limpio,
         pero el caller decide).
    """
    name = (c.get('name') or '').strip()
    domain = (c.get('domain') or '').lstrip('.').lower()

    for rx in DROP_NAME_PATTERNS:
        if rx.search(name): return 'drop'
    for suf in DROP_DOMAIN_SUFFIXES:
        if domain.endswith(suf.lstrip('.')): return 'drop'
    for rx in KEEP_NAME_PATTERNS:
        if rx.search(name): return 'keep'
    for suf in KEEP_DOMAIN_SUFFIXES:
        if domain.endswith(suf.lstrip('.')): return 'keep'
    return 'unknown'


def partition_cookies(cookies: Iterable[Dict]) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Reparte cookies en (keep, drop, unknown)."""
    keep, drop, unknown = [], [], []
    for c in cookies:
        cls = classify_cookie(c)
        if cls == 'keep': keep.append(c)
        elif cls == 'drop': drop.append(c)
        else: unknown.append(c)
    return keep, drop, unknown


def summarize(cookies: Iterable[Dict]) -> Dict[str, int]:
    keep, drop, unknown = partition_cookies(cookies)
    return {'keep': len(keep), 'drop': len(drop), 'unknown': len(unknown), 'total': len(keep)+len(drop)+len(unknown)}


if __name__ == '__main__':
    # demo
    sample = [
        {'name': 'TMUO', 'domain': '.ticketmaster.es'},
        {'name': '_ga', 'domain': '.ticketmaster.es'},
        {'name': 'OptanonConsent', 'domain': '.ticketmaster.es'},
        {'name': '_abck', 'domain': '.ticketmaster.es'},
        {'name': '_fbp', 'domain': '.ticketmaster.es'},
        {'name': 'cf_clearance', 'domain': '.ticketmaster.es'},
        {'name': 'random_cookie', 'domain': '.unknown.com'},
        {'name': 'reese84', 'domain': '.queue-it.net'},
    ]
    for c in sample:
        print(f'{classify_cookie(c):8} {c["name"]:30} {c["domain"]}')
    print('\nSummary:', summarize(sample))
