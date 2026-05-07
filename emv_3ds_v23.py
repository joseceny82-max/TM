"""emv_3ds_v23.py — EMV 3-D Secure v2.3 data fields builder.

EMV 3DS 2.3 (publicado 2023, mandatorio en EU desde 2025-2026) introduce
campos extra que permiten al ACS (Access Control Server) decidir
"frictionless" sin disparar challenge OTP/biometria.

Objetivo del modulo:
  - Construir el `AReq` (Authentication Request) JSON con los campos 2.3
  - Calcular heuristica de "challenge skip likelihood" (0-100)
  - Parsear la `ARes` (Authentication Response) y extraer indicadores
  - Persistencia json_atomic de configuracion + auditoria

Campos 2.3 nuevos (vs 2.2):
  - transType            : tipo de transaccion (01 goods, 03 check accept, ...)
  - payTokenInd          : indicador de pago tokenizado (Apple/Google Pay)
  - whitelistStatus      : merchant whitelisted por el cardholder
  - acsRendering         : ACS rendering hints (HTML/native)
  - decoupledMaxTime     : tiempo maximo decoupled auth (minutos)
  - challengeIndicatorOptional : 04, 05, 06, 07, 08 (nuevos opcionales 2.3)

API publica:
  build_v23_payload(card, browser_info, account_data) -> dict
  evaluate_challenge_skip_likelihood(payload) -> dict {score, signals}
  parse_2x_response(response_json) -> dict
  extract_challenge_indicator(response) -> dict
  get_config() / save_config()
  self_test()
  get_info()
"""
from __future__ import annotations
import os
import time
import hashlib
import secrets
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
import json_atomic


_CONFIG_KEY = "emv_3ds_v23_config.json"
_LOG_KEY = "emv_3ds_v23_log.json"

_lock = threading.RLock()


# 2.3 challenge indicator values (Spec Book 1, EMV 3DS 2.3)
CHALLENGE_INDICATORS = {
    "01": "no_preference",
    "02": "no_challenge_requested",
    "03": "challenge_requested_3ds_requestor",
    "04": "challenge_mandate",
    "05": "no_challenge_requested_TRA_performed",   # 2.3 new
    "06": "no_challenge_requested_data_share_only", # 2.3 new
    "07": "no_challenge_requested_strong_consumer_auth_already",  # 2.3 new
    "08": "no_challenge_requested_use_whitelist_exemption",       # 2.3 new
    "09": "challenge_requested_whitelist_promp",                  # 2.3 new
}

# 2.3 transType (ISO 18245)
TRANS_TYPES = {
    "01": "goods_or_service_purchase",
    "03": "check_acceptance",
    "10": "account_funding",
    "11": "quasi_cash_transaction",
    "28": "prepaid_activation_load",
}


# ===============================================================
# CONFIG
# ===============================================================
def get_config() -> Dict[str, Any]:
    return json_atomic.load(config.ruta(_CONFIG_KEY), default={
        "version": "2.3.1",
        "message_category": "01",            # 01 = PA (Payment Auth), 02 = NPA
        "default_trans_type": "01",
        "default_challenge_indicator": "05",  # frictionless preferido
        "include_browser_info": True,
        "include_decoupled": False,
        "decoupled_max_time_min": 30,
        "include_acs_rendering": True,
        "include_pay_token_ind": True,
        "tra_threshold_amount_eur": 30,
        "low_value_skip_threshold_eur": 30,  # PSD2/PSD3 LVT
    })


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_config()
    for k, v in (patch or {}).items():
        if k in cur:
            cur[k] = v
    json_atomic.save(config.ruta(_CONFIG_KEY), cur)
    return cur


def _append_log(entry: Dict[str, Any]) -> None:
    try:
        d = json_atomic.load(config.ruta(_LOG_KEY), default={"events": []})
        d["events"].insert(0, entry)
        d["events"] = d["events"][:300]
        json_atomic.save(config.ruta(_LOG_KEY), d)
    except Exception:
        pass


# ===============================================================
# PAYLOAD BUILDER
# ===============================================================
def _new_threeds_id() -> str:
    return ("tds-" + hashlib.sha1(
        f"{time.time()}{secrets.token_hex(4)}".encode()).hexdigest()[:24])


def _bin6(card_number: str) -> str:
    digits = "".join(c for c in (card_number or "") if c.isdigit())
    return digits[:6] if digits else ""


def _last4(card_number: str) -> str:
    digits = "".join(c for c in (card_number or "") if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else ""


def build_v23_payload(card: Dict[str, Any],
                      browser_info: Optional[Dict[str, Any]] = None,
                      account_data: Optional[Dict[str, Any]] = None,
                      amount: float = 0.0,
                      currency: str = "EUR",
                      pay_token_ind: bool = False,
                      whitelist_status: str = "U",
                      challenge_indicator: Optional[str] = None,
                      trans_type: Optional[str] = None) -> Dict[str, Any]:
    """Construye `AReq` EMV 3DS 2.3 JSON.

    Args:
      card           : {pan, exp_month, exp_year, brand}
      browser_info   : {user_agent, accept_header, ip, language,
                        color_depth, screen_w, screen_h, tz, java}
      account_data   : {acct_id, acct_age_days, num_purchases_6m, ...}
      amount         : float EUR (o currency dado)
      currency       : ISO-4217
      pay_token_ind  : bool, True si Apple/Google Pay token
      whitelist_status: 'Y','N','U','E' (PSD2 whitelist)
      challenge_indicator: '01'-'09' (default config)
      trans_type     : ISO 18245 (default '01')
    """
    cfg = get_config()
    card = card or {}
    browser_info = browser_info or {}
    account_data = account_data or {}

    ch_ind = challenge_indicator or cfg["default_challenge_indicator"]
    if ch_ind not in CHALLENGE_INDICATORS:
        ch_ind = "01"
    tt = trans_type or cfg["default_trans_type"]
    if tt not in TRANS_TYPES:
        tt = "01"

    threeds_id = _new_threeds_id()

    payload: Dict[str, Any] = {
        # 2.0/2.1 base fields
        "messageVersion": cfg["version"],
        "messageType": "AReq",
        "messageCategory": cfg["message_category"],
        "threeDSServerTransID": threeds_id,
        "deviceChannel": "02",     # 02 = Browser
        "acctNumber": _bin6(card.get("pan", "")) + "******" + _last4(card.get("pan", "")),
        "cardExpiryDate": (card.get("exp_year", "26")[-2:]
                           + str(card.get("exp_month", "12")).zfill(2)),
        "purchaseAmount": str(int(round((amount or 0.0) * 100))),
        "purchaseCurrency": currency,
        "purchaseDate": datetime.now().strftime("%Y%m%d%H%M%S"),
        "challengeWindowSize": "05",  # 05 = full screen

        # 2.3 NEW FIELDS
        "transType": tt,
        "payTokenInd": "01" if pay_token_ind else "02",
        "whitelistStatus": whitelist_status,   # Y/N/U/E
        "challengeIndicator": ch_ind,
        "challengeIndicatorOptional": (ch_ind if ch_ind in
                                       ("04", "05", "06", "07", "08", "09")
                                       else "01"),
    }

    # Decoupled (2.3 hint)
    if cfg.get("include_decoupled"):
        payload["decoupledAuthMaxTime"] = str(cfg.get("decoupled_max_time_min", 30))
        payload["decoupledAuthInd"] = "Y"

    # ACS rendering (2.3)
    if cfg.get("include_acs_rendering"):
        payload["acsRenderingType"] = {
            "acsInterface": "01",   # 01=native, 02=html
            "acsUiTemplate": "01",  # 01=text, 02=single-select, 03=multi
        }

    # Browser info block (2.x mandatory para deviceChannel=02)
    if cfg["include_browser_info"]:
        payload["browserInfo"] = {
            "browserUserAgent": browser_info.get("user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
            "browserAcceptHeader": browser_info.get("accept_header",
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            "browserIP": browser_info.get("ip", "127.0.0.1"),
            "browserLanguage": browser_info.get("language", "es-ES"),
            "browserColorDepth": str(browser_info.get("color_depth", 24)),
            "browserScreenHeight": str(browser_info.get("screen_h", 1080)),
            "browserScreenWidth": str(browser_info.get("screen_w", 1920)),
            "browserTZ": str(browser_info.get("tz", -60)),
            "browserJavaEnabled": browser_info.get("java", False),
            "browserJavascriptEnabled": True,
        }

    # Account info
    if account_data:
        payload["acctInfo"] = {
            "chAccAgeInd": _age_band(account_data.get("acct_age_days", 0)),
            "chAccChange": account_data.get("acct_last_change_date", ""),
            "chAccDate": account_data.get("acct_create_date", ""),
            "chAccPwChange": account_data.get("acct_pw_change_date", ""),
            "nbPurchaseAccount": str(account_data.get("num_purchases_6m", 0)),
            "provisionAttemptsDay": str(account_data.get("provision_attempts_24h", 0)),
            "txnActivityDay": str(account_data.get("txn_24h", 0)),
            "txnActivityYear": str(account_data.get("txn_year", 0)),
            "paymentAccAge": _age_band(account_data.get("payment_acct_age_days", 0)),
        }

    # Marca tras meterlo en cache
    _append_log({
        "event": "build_payload",
        "threeds_id": threeds_id,
        "amount": amount,
        "currency": currency,
        "challenge_indicator": ch_ind,
        "ts": datetime.now().isoformat(),
    })

    return payload


def _age_band(days: int) -> str:
    """01 = no account, 02 = created during txn, 03 = <30d, 04 = 30-60d, 05 = >60d."""
    if days <= 0:
        return "01"
    if days < 1:
        return "02"
    if days < 30:
        return "03"
    if days < 60:
        return "04"
    return "05"


# ===============================================================
# CHALLENGE-SKIP LIKELIHOOD
# ===============================================================
def evaluate_challenge_skip_likelihood(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Heuristica 0-100 de probabilidad de que el ACS NO pida challenge.

    Mas alto = mas probable frictionless. ACS reales suelen agregar
    senales similares con scoring propio.
    """
    if not payload:
        return {"ok": False, "error": "payload_required"}

    score = 50
    signals: List[Dict[str, Any]] = []
    cfg = get_config()

    # 1. Challenge indicator
    ch = payload.get("challengeIndicator", "01")
    if ch in ("05", "06", "07", "08"):
        score += 25
        signals.append({"name": "challenge_indicator_no_challenge", "delta": +25})
    elif ch == "01":
        score += 5
        signals.append({"name": "challenge_indicator_no_pref", "delta": +5})
    elif ch in ("03", "04", "09"):
        score -= 35
        signals.append({"name": "challenge_indicator_requested", "delta": -35})

    # 2. Whitelist status
    ws = payload.get("whitelistStatus", "U")
    if ws == "Y":
        score += 30
        signals.append({"name": "merchant_whitelisted", "delta": +30})
    elif ws == "E":
        score -= 5
        signals.append({"name": "whitelist_pending", "delta": -5})

    # 3. Pay token (Apple/Google Pay -> usually skip)
    if payload.get("payTokenInd") == "01":
        score += 15
        signals.append({"name": "pay_token_apple_google", "delta": +15})

    # 4. Low value (< 30 EUR PSD2 LVT exemption)
    try:
        amount = int(payload.get("purchaseAmount", "0")) / 100.0
    except Exception:
        amount = 0
    if 0 < amount < cfg["low_value_skip_threshold_eur"]:
        score += 10
        signals.append({"name": "low_value_lvt", "delta": +10, "amount": amount})

    # 5. Account age signal
    age_band = (payload.get("acctInfo") or {}).get("chAccAgeInd", "01")
    if age_band == "05":
        score += 10
        signals.append({"name": "old_trusted_account", "delta": +10})
    elif age_band in ("01", "02", "03"):
        score -= 10
        signals.append({"name": "new_account", "delta": -10})

    # 6. Browser info present
    if "browserInfo" in payload:
        score += 5
        signals.append({"name": "browser_info_present", "delta": +5})

    # 7. ACS rendering hints
    if "acsRenderingType" in payload:
        score += 3
        signals.append({"name": "acs_rendering_hint", "delta": +3})

    # 8. transType goods (01) is most benign
    if payload.get("transType") == "01":
        score += 2
        signals.append({"name": "trans_type_goods", "delta": +2})

    # Clamp
    score = max(0, min(100, score))

    likely_skip = score >= 70
    return {"ok": True,
            "skip_score": score,
            "likely_frictionless": likely_skip,
            "signals": signals,
            "version": payload.get("messageVersion"),
            "amount_eur": amount}


# ===============================================================
# RESPONSE PARSER
# ===============================================================
def parse_2x_response(response_json: Dict[str, Any]) -> Dict[str, Any]:
    """Parsea ARes / RReq / ChallengeResponse en formato 2.x.

    Devuelve estado normalizado: status (Y/N/U/A/C/R/D/I), eci, cavv, etc.
    """
    if not isinstance(response_json, dict):
        return {"ok": False, "error": "invalid_response"}

    r = response_json
    status = r.get("transStatus", "U")
    reason = r.get("transStatusReason", "")
    eci = r.get("eci", "")
    cavv = r.get("authenticationValue", "") or r.get("cavv", "")
    ds_id = r.get("dsTransID", "")

    # Decision
    decision = {
        "Y": "frictionless_authenticated",
        "N": "not_authenticated",
        "U": "auth_could_not_be_performed",
        "A": "attempt_processed",
        "C": "challenge_required",
        "R": "rejected",
        "D": "decoupled",         # 2.3
        "I": "informational",     # 2.3
    }.get(status, "unknown")

    requires_challenge = (status == "C")

    return {"ok": True,
            "trans_status": status,
            "trans_status_reason": reason,
            "decision": decision,
            "requires_challenge": requires_challenge,
            "eci": eci,
            "cavv": cavv,
            "ds_trans_id": ds_id,
            "message_version": r.get("messageVersion"),
            "threeds_server_trans_id": r.get("threeDSServerTransID")}


def extract_challenge_indicator(response: Dict[str, Any]) -> Dict[str, Any]:
    """De una ARes/AResExtension extrae el challengeIndicator usado/sugerido."""
    if not isinstance(response, dict):
        return {"ok": False, "error": "invalid_response"}
    ch = response.get("challengeIndicator") or response.get("acsChallengeIndicator")
    if not ch:
        return {"ok": True, "indicator": None,
                "meaning": "not_present"}
    return {"ok": True,
            "indicator": ch,
            "meaning": CHALLENGE_INDICATORS.get(ch, "unknown_value"),
            "is_2_3_optional": ch in ("04", "05", "06", "07", "08", "09")}


# ===============================================================
# SELF TEST
# ===============================================================
def self_test() -> Dict[str, Any]:
    checks: Dict[str, bool] = {}

    # 1. Config
    cfg = get_config()
    checks["config_version_23"] = cfg["version"].startswith("2.3")
    checks["config_default_indicator"] = "default_challenge_indicator" in cfg

    # 2. Build basic payload
    p = build_v23_payload(
        card={"pan": "4111111111111111", "exp_month": "12", "exp_year": "2028"},
        browser_info={"user_agent": "Mozilla/5.0 Test", "ip": "1.2.3.4",
                      "language": "es-ES", "color_depth": 24,
                      "screen_w": 1920, "screen_h": 1080, "tz": -60},
        account_data={"acct_age_days": 200, "num_purchases_6m": 12,
                       "txn_24h": 1, "txn_year": 25},
        amount=120.50, currency="EUR")
    checks["payload_has_msg_version"] = p["messageVersion"].startswith("2.3")
    checks["payload_has_threeds_id"] = p["threeDSServerTransID"].startswith("tds-")
    checks["payload_has_trans_type"] = "transType" in p
    checks["payload_has_pay_token_ind"] = "payTokenInd" in p
    checks["payload_has_whitelist"] = "whitelistStatus" in p
    checks["payload_has_acs_rendering"] = "acsRenderingType" in p
    checks["payload_has_challenge_opt"] = "challengeIndicatorOptional" in p
    checks["payload_browser_info"] = "browserInfo" in p
    checks["payload_acct_info"] = "acctInfo" in p
    checks["payload_amount_centimos"] = p["purchaseAmount"] == "12050"

    # 3. Build with pay_token_ind=True and whitelist=Y
    p2 = build_v23_payload(
        card={"pan": "5555000011112222", "exp_month": "06", "exp_year": "2027"},
        amount=10.00,
        pay_token_ind=True,
        whitelist_status="Y",
        challenge_indicator="08",
        trans_type="01")
    checks["pay_token_yes"] = p2["payTokenInd"] == "01"
    checks["whitelist_y"] = p2["whitelistStatus"] == "Y"
    checks["challenge_indicator_08"] = p2["challengeIndicator"] == "08"

    # 4. Build with invalid challenge_indicator -> falls back to '01'
    p3 = build_v23_payload(
        card={"pan": "4242424242424242"},
        challenge_indicator="99_invalid")
    checks["invalid_indicator_falls_back"] = p3["challengeIndicator"] == "01"

    # 5. Build with invalid trans_type -> '01'
    p4 = build_v23_payload(card={"pan": "4242424242424242"},
                           trans_type="99")
    checks["invalid_trans_type_fallback"] = p4["transType"] == "01"

    # 6. Evaluate skip likelihood (high score)
    e_high = evaluate_challenge_skip_likelihood(p2)
    checks["evaluate_high_ok"] = e_high.get("ok") is True
    checks["evaluate_high_score"] = e_high.get("skip_score", 0) >= 70
    checks["evaluate_high_frictionless"] = (e_high.get("likely_frictionless")
                                            is True)
    checks["evaluate_signals_listed"] = isinstance(e_high.get("signals"), list)

    # 7. Evaluate skip likelihood (low - challenge mandate)
    p_low = build_v23_payload(
        card={"pan": "4111111111111111"},
        amount=900,
        whitelist_status="N",
        challenge_indicator="04",
        account_data={"acct_age_days": 1})
    e_low = evaluate_challenge_skip_likelihood(p_low)
    checks["evaluate_low_score"] = e_low.get("skip_score", 100) <= 50
    checks["evaluate_low_no_friction"] = (e_low.get("likely_frictionless")
                                          is False)

    # 8. Parse 2.x response Y
    resp_y = {"messageVersion": "2.3.1",
              "transStatus": "Y", "eci": "05",
              "authenticationValue": "AAABCSIIAAAAA...",
              "threeDSServerTransID": "tds-abc",
              "dsTransID": "ds-xyz"}
    pr = parse_2x_response(resp_y)
    checks["parse_y_ok"] = pr.get("ok") is True
    checks["parse_y_authenticated"] = pr.get("decision") == "frictionless_authenticated"
    checks["parse_y_no_challenge"] = pr.get("requires_challenge") is False
    checks["parse_y_eci"] = pr.get("eci") == "05"

    # 9. Parse 2.x response C (challenge)
    resp_c = {"messageVersion": "2.3.1", "transStatus": "C"}
    pr2 = parse_2x_response(resp_c)
    checks["parse_c_requires_challenge"] = pr2.get("requires_challenge") is True

    # 10. Parse 2.3 D (decoupled)
    resp_d = {"messageVersion": "2.3.1", "transStatus": "D"}
    pr3 = parse_2x_response(resp_d)
    checks["parse_d_decoupled"] = pr3.get("decision") == "decoupled"

    # 11. Parse invalid input
    pr_bad = parse_2x_response("not_a_dict")
    checks["parse_bad_input"] = pr_bad.get("ok") is False

    # 12. Extract challenge indicator
    ext = extract_challenge_indicator({"challengeIndicator": "05"})
    checks["extract_05_meaning"] = ("TRA_performed" in ext.get("meaning", "")
                                    or ext.get("meaning") ==
                                    CHALLENGE_INDICATORS["05"])
    checks["extract_05_is_2_3_opt"] = ext.get("is_2_3_optional") is True
    ext2 = extract_challenge_indicator({})
    checks["extract_missing"] = ext2.get("indicator") is None

    # 13. Save / load config roundtrip
    saved = save_config({"default_trans_type": "01"})
    checks["save_config_returns_dict"] = isinstance(saved, dict)
    checks["save_config_persisted"] = (get_config()["default_trans_type"]
                                       == "01")

    total = len(checks)
    passed = sum(1 for v in checks.values() if v)
    return {"ok": passed == total,
            "total_checks": total,
            "passed_checks": passed,
            "checks": checks}


def get_info() -> Dict[str, Any]:
    return {
        "module": "emv_3ds_v23",
        "version": 1,
        "purpose": "EMV 3DS v2.3 AReq builder + challenge-skip heuristic",
        "spec_version": get_config()["version"],
        "valid_challenge_indicators": list(CHALLENGE_INDICATORS.keys()),
        "valid_trans_types": list(TRANS_TYPES.keys()),
    }
