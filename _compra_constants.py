"""_compra_constants.py — Constantes centralizadas para el bot de compra.

Objetivo: eliminar magic numbers diseminados en `_compra_parallel.py` y módulos
relacionados. Agrupa timeouts, intervalos y límites por CATEGORÍA SEMÁNTICA,
no por valor absoluto.

Convención de nombres:
    TIMEOUT_*         — espera máxima antes de dar por fallida una operación
    POLL_*            — intervalo de polling dentro de un loop
    RETRY_*           — número de intentos
    DELAY_*           — pausa intencionada (no espera de operación)
    BUDGET_*          — tiempo total presupuestado para una fase
    LIMIT_*           — capacidades / umbrales

Tiers semánticos:
    FAST   = operaciones de UI inmediatas (clicks, fill inputs, lecturas DOM)
    NORMAL = navegaciones DOM-ready, modales, opciones de combobox
    SLOW   = navegaciones cross-domain, OAuth redirect, hidratación SPA
    LONG   = recovery IMAP, captcha humano, queue-it

Reglas de oro:
    1. Cualquier nuevo magic number en código DEBE ir aquí con nombre semántico.
    2. NO importar este módulo dentro de funciones — siempre top-level.
    3. NO duplicar constantes; reusar las existentes incluso si el valor coincide
       casualmente (la semántica es lo que importa).

IMPORTANTE:
    Esta primera versión SOLO define las constantes; NO sustituye los valores
    en `_compra_parallel.py` para no riesgar comportamiento. La migración será
    incremental y revisada en próximas sesiones.
"""

# ════════════════════════════════════════════════════════════════════
#  NETWORK / HTTP — timeouts de I/O externo
# ════════════════════════════════════════════════════════════════════
TIMEOUT_HTTP_FAST_S = 8           # MoreLogin API local /json/version, /api/env/*
TIMEOUT_HTTP_NORMAL_S = 15        # cuRL_cffi GET a TM home / pre_flight
TIMEOUT_HTTP_PROXY_S = 12         # request via Quantum (latencia residencial)
TIMEOUT_HTTP_REPUTATION_S = 8     # ip-api.com / ipify

# ════════════════════════════════════════════════════════════════════
#  BROWSER NAVIGATION — page.goto / wait_for_navigation
# ════════════════════════════════════════════════════════════════════
TIMEOUT_GOTO_HOME_MS = 45_000     # www.ticketmaster.es/ (puede tardar por EPS)
TIMEOUT_GOTO_EVENT_MS = 30_000    # /event/... DOM-ready
TIMEOUT_GOTO_AUTH_MS = 30_000     # auth.ticketmaster.com OAuth
TIMEOUT_GOTO_CHECKOUT_MS = 30_000 # /checkout/...
TIMEOUT_GOTO_RECOVERY_MS = 20_000 # account_recovery navegación

# ════════════════════════════════════════════════════════════════════
#  UI INTERACTIONS — waits de elementos visibles / acciones DOM
# ════════════════════════════════════════════════════════════════════
TIMEOUT_UI_FAST_MS = 1_500        # banner cookies, modal pequeño
TIMEOUT_UI_NORMAL_MS = 3_000      # botón Continuar, click Acepto, OTP input
TIMEOUT_UI_SLOW_MS = 8_000        # stepper visible, find_tickets ready
TIMEOUT_UI_FORM_FILL_MS = 5_000   # tipo email/password en form

TIMEOUT_SELECTOR_FAST_MS = 2_000  # locator.is_visible quick check
TIMEOUT_SELECTOR_NORMAL_MS = 5_000
TIMEOUT_SELECTOR_SLOW_MS = 15_000 # quantityStepper (puede tardar)

# ════════════════════════════════════════════════════════════════════
#  AUTH / OAUTH / RECOVERY — flujo de login
# ════════════════════════════════════════════════════════════════════
TIMEOUT_EMAIL_INPUT_MS = 25_000   # PingFederate render del form email
TIMEOUT_PWD_INPUT_MS = 25_000     # PingFederate render del form password
TIMEOUT_OTP_INPUT_MS = 3_000      # MFA OTP input visible (post-paste)
BUDGET_OTP_EMAIL_S = 180          # max espera email OTP via IMAP
BUDGET_RECOVERY_IMAP_S = 240      # max espera reset email via IMAP madre
BUDGET_LOGIN_TOTAL_S = 90         # presupuesto total de un intento de login

# ════════════════════════════════════════════════════════════════════
#  CHECKOUT / PAYMENT — Adyen iframe, fill card, 3DS
# ════════════════════════════════════════════════════════════════════
TIMEOUT_ADYEN_IFRAME_READY_MS = 10_000  # iframe payments-cc cargado
TIMEOUT_ADYEN_FIELD_VISIBLE_MS = 3_000  # input cardNumber/expiry/cvv
TIMEOUT_PROVINCE_COMBOBOX_MS = 2_000    # combobox React abre dropdown
BUDGET_FILL_CARD_S = 60                 # presupuesto total fill_card
BUDGET_3DS_S = 120                      # presupuesto total challenge 3DS

# ════════════════════════════════════════════════════════════════════
#  POLLING INTERVALS — frecuencias de re-check
# ════════════════════════════════════════════════════════════════════
POLL_FAST_MS = 200          # paste OTP, espera de re-render React
POLL_NORMAL_MS = 500        # iframe payments-cc poll, watcher URL change
POLL_SLOW_MS = 1_000        # checkout stages, queue-it position
POLL_QUEUE_IT_MS = 2_000    # queue-it body check
POLL_STOP_FLAG_MS = 5_000   # check `_compra_parallel.STOP` flag

# ════════════════════════════════════════════════════════════════════
#  RETRY COUNTS — número de intentos por operación
# ════════════════════════════════════════════════════════════════════
RETRY_PROFILE_PICK = 5         # pick_morelogin_profile (5 swaps)
RETRY_PROFILE_START = 5        # _start_profile (5 attempts MoreLogin API)
RETRY_CLICK_CONTINUAR = 3      # click Continuar Comprando (3 = mayor tolerancia transient TM)
RETRY_COOKIES_BANNER = 3       # post-hydration cookies accept
RETRY_IFRAME_POLL = 20         # iframe payments-cc (20 × POLL_NORMAL = 10s)
RETRY_ANADIR_TARJETA = 16      # poll Añadir tarjeta (16 × 500ms = 8s)
RETRY_SEPARATED_SEATS = 3      # forzar asientos consecutivos
RETRY_CHECKOUT_STAGES = 8      # watch checkout URL change

# ════════════════════════════════════════════════════════════════════
#  HUMAN-LIKE DELAYS — pausas intencionadas para parecer humano
# ════════════════════════════════════════════════════════════════════
DELAY_TYPING_MIN_MS = 20       # delay entre tecla y tecla (mínimo)
DELAY_TYPING_MAX_MS = 50       # idem (máximo)
DELAY_BETWEEN_FIELDS_MIN_S = 0.5
DELAY_BETWEEN_FIELDS_MAX_S = 1.2
DELAY_AFTER_CLICK_MIN_S = 0.6
DELAY_AFTER_CLICK_MAX_S = 1.4
DELAY_BROWSER_INIT_S = 4       # espera tras start_profile antes de CDP connect

# ════════════════════════════════════════════════════════════════════
#  EPS / TM-SPECIFIC — warming, eps_sid, queue-it
# ════════════════════════════════════════════════════════════════════
DELAY_EPS_WARMING_HOME_S = 8   # tiempo esperando /eps-mgr + eps.js
DELAY_POST_NAVIGATION_S = 6    # estabilización post-goto
BUDGET_EPS_CHALLENGE_S = 25    # tiempo max para /epsf/gec/v3/Event challenge
BUDGET_QUEUE_IT_S = 1_800      # 30 min queue-it (raro pero soportado)

# ════════════════════════════════════════════════════════════════════
#  STORAGE STATE — gestión de cookies persistidas por cuenta
# ════════════════════════════════════════════════════════════════════
MAX_STORAGE_STATE_AGE_S = 4 * 3600   # 4h — eps_sid bind a IP, ver fix 2026-05-13
MAX_BAD_CREDS_AGE_DAYS = 14          # cuántos días antes de re-intentar bad_creds

# ════════════════════════════════════════════════════════════════════
#  LOCKS / RACE PREVENTION
# ════════════════════════════════════════════════════════════════════
TIMEOUT_LOCK_ACQUIRE_S = 30    # _acquire_pick_mutex max wait
TTL_LOCK_STALE_S = 60          # lock file > 60s = stale, override

# ════════════════════════════════════════════════════════════════════
#  OBSERVABILITY — truncado de strings en logs
# ════════════════════════════════════════════════════════════════════
TRUNC_LOG_ERR_SHORT = 80       # mensajes de error cortos (try/except simple)
TRUNC_LOG_ERR_NORMAL = 120     # errores estándar
TRUNC_LOG_ERR_LONG = 200       # errores con stack relevante
TRUNC_LOG_URL = 80             # URLs en logs
TRUNC_LOG_URL_LONG = 200       # URLs en captura forense
TRUNC_LOG_BODY_PREVIEW = 400   # primeros chars del body en logs

# ════════════════════════════════════════════════════════════════════
#  LIMITS — capacidades absolutas
# ════════════════════════════════════════════════════════════════════
LIMIT_ACCOUNT_SWAPS = 5        # max account swaps por intento de compra
LIMIT_PROFILE_BURN_PER_RUN = 10  # max perfiles consumidos por run del wrapper
LIMIT_SCREENSHOTS_PER_RUN = 50   # max screenshots antes de skip (anti spam)


# ────────────────────────────────────────────────────────────────────
#  EXIT CODES — semántica de return de main()
# ────────────────────────────────────────────────────────────────────
RC_SUCCESS = 0
RC_FATAL = 1
RC_BOT_BLOCK = 99   # wrapper detecta y reintenta con swap


# ────────────────────────────────────────────────────────────────────
#  Auto-test mínimo
# ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Imprime todas las constantes para revisión humana
    import sys
    _mod = sys.modules[__name__]
    print(f"=== _compra_constants.py — {sum(1 for k in dir(_mod) if k.isupper())} constantes ===")
    for k in sorted(dir(_mod)):
        if k.startswith("_") or not k.isupper() and not k.startswith(("TIMEOUT", "POLL", "RETRY", "DELAY", "BUDGET", "LIMIT", "TRUNC", "MAX", "TTL", "RC")):
            continue
        v = getattr(_mod, k)
        if isinstance(v, (int, float, str, bool)):
            print(f"  {k:40} = {v}")
