"""
Configuracion central del proyecto TM PERROS.

Dashboard multi-dominio para Ticketmaster (todos los paises soportados).
Estructura de `datos/` espeja las 18 pestanas del dashboard.
"""
import os

# ==========================================
#  RAIZ DEL PROYECTO
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==========================================
#  CARPETAS PRINCIPALES
# ==========================================
DATOS_DIR   = os.path.join(BASE_DIR, "datos")
LOGS_DIR    = os.path.join(BASE_DIR, "logs")
SECRETS_DIR = os.path.join(BASE_DIR, "secrets")

# ==========================================
#  CARPETAS DE DATOS (una por pestana)
# ==========================================
DATOS_DASHBOARD      = os.path.join(DATOS_DIR, "dashboard")
DATOS_PROBLEMAS      = os.path.join(DATOS_DIR, "problemas")
DATOS_EVENTOS        = os.path.join(DATOS_DIR, "eventos")
DATOS_SITIOS         = os.path.join(DATOS_DIR, "sitios")
DATOS_COMPRAS        = os.path.join(DATOS_DIR, "compras")
DATOS_SNIPER_BOT     = os.path.join(DATOS_DIR, "sniper_bot")
DATOS_CUENTAS        = os.path.join(DATOS_DIR, "cuentas")
DATOS_FILAS          = os.path.join(DATOS_DIR, "filas")
DATOS_COOKIES        = os.path.join(DATOS_DIR, "cookies")
DATOS_ANALYTICS      = os.path.join(DATOS_DIR, "analytics")
DATOS_NOTIFICACIONES = os.path.join(DATOS_DIR, "notificaciones")
DATOS_MONITOR        = os.path.join(DATOS_DIR, "monitor")
DATOS_CONSOLA        = os.path.join(DATOS_DIR, "consola")
DATOS_BACKUP         = os.path.join(DATOS_DIR, "backup")
DATOS_USUARIOS       = os.path.join(DATOS_DIR, "usuarios")
DATOS_OTP            = os.path.join(DATOS_DIR, "otp")
DATOS_SOPORTE        = os.path.join(DATOS_DIR, "soporte")
DATOS_NOTAS          = os.path.join(DATOS_DIR, "notas")
DATOS_NOTAS_FILES    = os.path.join(DATOS_NOTAS, "files")
DATOS_CONFIG_APP     = os.path.join(DATOS_DIR, "config_app")
DATOS_DOMINIOS       = os.path.join(DATOS_DIR, "dominios")
DATOS_ESTADO         = os.path.join(DATOS_DIR, "estado")

# Subcarpetas Sniper Bot
SB_PROXIES     = os.path.join(DATOS_SNIPER_BOT, "proxies")
SB_ANTIBOT     = os.path.join(DATOS_SNIPER_BOT, "antibot")
SB_NAVEGADORES = os.path.join(DATOS_SNIPER_BOT, "navegadores")
SB_EXTENSIONES = os.path.join(DATOS_SNIPER_BOT, "extensiones")
SB_BYPASS      = os.path.join(DATOS_SNIPER_BOT, "bypass")
SB_CAPTCHA     = os.path.join(DATOS_SNIPER_BOT, "captcha")
SB_SCREENSHOTS = os.path.join(DATOS_SNIPER_BOT, "screenshots")

COOKIES_POOL   = os.path.join(DATOS_COOKIES, "pool")
FILAS_SESSIONS = os.path.join(DATOS_FILAS, "sessions")

# ==========================================
#  DOMINIOS TICKETMASTER POR PAIS
# ==========================================
# Lista maestra de todos los dominios oficiales de Ticketmaster.
# El usuario puede seleccionar uno, varios o todos desde el dashboard.
TM_DOMAINS = {
    # NORTE AMERICA
    "us":       {"domain": "www.ticketmaster.com",        "country": "Estados Unidos",   "country_en": "United States",    "flag": "us", "currency": "USD", "lang": "en-us", "region": "NA"},
    "ca":       {"domain": "www.ticketmaster.ca",         "country": "Canada",           "country_en": "Canada",            "flag": "ca", "currency": "CAD", "lang": "en-ca", "region": "NA"},
    "mx":       {"domain": "www.ticketmaster.com.mx",     "country": "Mexico",           "country_en": "Mexico",            "flag": "mx", "currency": "MXN", "lang": "es-mx", "region": "NA"},

    # EUROPA
    "es":       {"domain": "www.ticketmaster.es",         "country": "Espana",           "country_en": "Spain",             "flag": "es", "currency": "EUR", "lang": "es-es", "region": "EU"},
    "uk":       {"domain": "www.ticketmaster.co.uk",      "country": "Reino Unido",      "country_en": "United Kingdom",    "flag": "gb", "currency": "GBP", "lang": "en-gb", "region": "EU"},
    "ie":       {"domain": "www.ticketmaster.ie",         "country": "Irlanda",          "country_en": "Ireland",           "flag": "ie", "currency": "EUR", "lang": "en-ie", "region": "EU"},
    "de":       {"domain": "www.ticketmaster.de",         "country": "Alemania",         "country_en": "Germany",           "flag": "de", "currency": "EUR", "lang": "de-de", "region": "EU"},
    "at":       {"domain": "www.ticketmaster.at",         "country": "Austria",          "country_en": "Austria",           "flag": "at", "currency": "EUR", "lang": "de-at", "region": "EU"},
    "ch":       {"domain": "www.ticketmaster.ch",         "country": "Suiza",            "country_en": "Switzerland",       "flag": "ch", "currency": "CHF", "lang": "de-ch", "region": "EU"},
    "fr":       {"domain": "www.ticketmaster.fr",         "country": "Francia",          "country_en": "France",            "flag": "fr", "currency": "EUR", "lang": "fr-fr", "region": "EU"},
    "be_fr":    {"domain": "www.ticketmaster.be",         "country": "Belgica (FR)",     "country_en": "Belgium (FR)",      "flag": "be", "currency": "EUR", "lang": "fr-be", "region": "EU"},
    "be_nl":    {"domain": "www.ticketmaster.be/nl",      "country": "Belgica (NL)",     "country_en": "Belgium (NL)",      "flag": "be", "currency": "EUR", "lang": "nl-be", "region": "EU"},
    "nl":       {"domain": "www.ticketmaster.nl",         "country": "Paises Bajos",     "country_en": "Netherlands",       "flag": "nl", "currency": "EUR", "lang": "nl-nl", "region": "EU"},
    "it":       {"domain": "www.ticketmaster.it",         "country": "Italia",           "country_en": "Italy",             "flag": "it", "currency": "EUR", "lang": "it-it", "region": "EU"},
    "pt":       {"domain": "www.ticketmaster.pt",         "country": "Portugal",         "country_en": "Portugal",          "flag": "pt", "currency": "EUR", "lang": "pt-pt", "region": "EU"},
    "dk":       {"domain": "www.ticketmaster.dk",         "country": "Dinamarca",        "country_en": "Denmark",           "flag": "dk", "currency": "DKK", "lang": "da-dk", "region": "EU"},
    "no":       {"domain": "www.ticketmaster.no",         "country": "Noruega",          "country_en": "Norway",            "flag": "no", "currency": "NOK", "lang": "no-no", "region": "EU"},
    "se":       {"domain": "www.ticketmaster.se",         "country": "Suecia",           "country_en": "Sweden",            "flag": "se", "currency": "SEK", "lang": "sv-se", "region": "EU"},
    "fi":       {"domain": "www.ticketmaster.fi",         "country": "Finlandia",        "country_en": "Finland",           "flag": "fi", "currency": "EUR", "lang": "fi-fi", "region": "EU"},
    "pl":       {"domain": "www.ticketmaster.pl",         "country": "Polonia",          "country_en": "Poland",            "flag": "pl", "currency": "PLN", "lang": "pl-pl", "region": "EU"},
    "cz":       {"domain": "www.ticketmaster.cz",         "country": "Republica Checa",  "country_en": "Czech Republic",    "flag": "cz", "currency": "CZK", "lang": "cs-cz", "region": "EU"},
    "gr":       {"domain": "www.ticketmaster.gr",         "country": "Grecia",           "country_en": "Greece",            "flag": "gr", "currency": "EUR", "lang": "el-gr", "region": "EU"},

    # OCEANIA
    "au":       {"domain": "www.ticketmaster.com.au",     "country": "Australia",        "country_en": "Australia",         "flag": "au", "currency": "AUD", "lang": "en-au", "region": "OC"},
    "nz":       {"domain": "www.ticketmaster.co.nz",      "country": "Nueva Zelanda",    "country_en": "New Zealand",       "flag": "nz", "currency": "NZD", "lang": "en-nz", "region": "OC"},

    # MEDIO ORIENTE
    "ae":       {"domain": "www.ticketmaster.ae",         "country": "EAU",              "country_en": "UAE",               "flag": "ae", "currency": "AED", "lang": "en-ae", "region": "ME"},
}

# Dominio por defecto si el usuario no selecciona
TM_DEFAULT_DOMAIN = "us"

# ==========================================
#  ARCHIVOS VIVOS
# ==========================================
VIVO_CUENTAS     = os.path.join(DATOS_CUENTAS, "cuentas.csv")
VIVO_TARJETAS    = os.path.join(DATOS_CUENTAS, "tarjetas.csv")
VIVO_PROXIES     = os.path.join(SB_PROXIES, "proxies.txt")
VIVO_EVENTOS     = os.path.join(DATOS_EVENTOS, "eventos.csv")
VIVO_USUARIOS    = os.path.join(DATOS_USUARIOS, "usuarios.csv")
VIVO_SOPORTE     = os.path.join(DATOS_SOPORTE, "tickets.csv")
VIVO_PROBLEMAS   = os.path.join(DATOS_PROBLEMAS, "problemas.csv")
VIVO_OTP         = os.path.join(DATOS_OTP, "otp.csv")
VIVO_COOKIES     = os.path.join(DATOS_COOKIES, "cookies.csv")
VIVO_EXTENSIONES = os.path.join(SB_EXTENSIONES, "extensiones.csv")
VIVO_COMPRAS     = os.path.join(DATOS_COMPRAS, "compras.csv")
VIVO_SETTINGS    = os.path.join(DATOS_CONFIG_APP, "settings.json")
VIVO_IMAP        = os.path.join(DATOS_OTP, "imap.csv")
VIVO_DOMINIOS    = os.path.join(DATOS_DOMINIOS, "seleccionados.json")

# ==========================================
#  MAPA DE RUTAS
# ==========================================
RUTAS = {
    "dashboard_data.json":    os.path.join(DATOS_DASHBOARD, "dashboard_data.json"),
    "events.json":            os.path.join(DATOS_EVENTOS, "events.json"),
    "accounts.json":          os.path.join(DATOS_CUENTAS, "accounts.json"),
    "cards.json":             os.path.join(DATOS_CUENTAS, "cards.json"),
    "purchases.json":         os.path.join(DATOS_COMPRAS, "purchases.json"),
    "proxies.json":           os.path.join(SB_PROXIES, "proxies.json"),
    "antibot.json":           os.path.join(SB_ANTIBOT, "antibot.json"),
    "browsers.json":          os.path.join(SB_NAVEGADORES, "browsers.json"),
    "extensions.json":        os.path.join(SB_EXTENSIONES, "extensions.json"),
    "bypass.json":            os.path.join(SB_BYPASS, "bypass.json"),
    "captcha.json":           os.path.join(SB_CAPTCHA, "captcha.json"),
    "notifications.json":     os.path.join(DATOS_NOTIFICACIONES, "notifications.json"),
    "monitor_data.json":      os.path.join(DATOS_MONITOR, "monitor_data.json"),
    "analytics_data.json":    os.path.join(DATOS_ANALYTICS, "analytics_data.json"),
    "users.json":             os.path.join(DATOS_USUARIOS, "users.json"),
    "otp.json":               os.path.join(DATOS_OTP, "otp.json"),
    "tickets.json":           os.path.join(DATOS_SOPORTE, "tickets.json"),
    "problems.json":          os.path.join(DATOS_PROBLEMAS, "problems.json"),
    "notes.json":             os.path.join(DATOS_NOTAS, "notes.json"),
    "settings.json":          os.path.join(DATOS_CONFIG_APP, "settings.json"),
    "sites.json":             os.path.join(DATOS_SITIOS, "sites.json"),
    "domains_selected.json":  os.path.join(DATOS_DOMINIOS, "seleccionados.json"),
    "bot_workers.json":       os.path.join(DATOS_SNIPER_BOT, "workers.json"),
    "bot_groups.json":        os.path.join(DATOS_SNIPER_BOT, "groups.json"),
    "queues.json":            os.path.join(DATOS_FILAS, "queues.json"),
    "cookies.json":           os.path.join(DATOS_COOKIES, "cookies.json"),

    # --- Estado suelto (antes en raiz) ---
    "proxy_health.json":              os.path.join(DATOS_ESTADO, "proxy_health.json"),
    "acpat_pool.json":                os.path.join(DATOS_ESTADO, "acpat_pool.json"),
    "cookie_warm_log.json":           os.path.join(DATOS_ESTADO, "cookie_warm_log.json"),
    "behavior_chain_matrix.json":     os.path.join(DATOS_ESTADO, "behavior_chain_matrix.json"),
    "behavior_chain_log.json":        os.path.join(DATOS_ESTADO, "behavior_chain_log.json"),
    "event_discovery.json":           os.path.join(DATOS_ESTADO, "event_discovery.json"),
    "featured_cache.json":            os.path.join(DATOS_ESTADO, "featured_cache.json"),
    "health_monitor.json":            os.path.join(DATOS_ESTADO, "health_monitor.json"),
    "captcha_budget.json":            os.path.join(DATOS_ESTADO, "captcha_budget.json"),
    "hyper_sdk_chain_config.json":    os.path.join(DATOS_ESTADO, "hyper_sdk_chain_config.json"),
    "hyper_sdk_chain_stats.json":     os.path.join(DATOS_ESTADO, "hyper_sdk_chain_stats.json"),
    "hyper_sdk_chain_log.json":       os.path.join(DATOS_ESTADO, "hyper_sdk_chain_log.json"),
    "imperva_cache.json":             os.path.join(DATOS_ESTADO, "imperva_cache.json"),
    "otp_3ds_config.json":            os.path.join(DATOS_ESTADO, "otp_3ds_config.json"),
    "otp_3ds_log.json":               os.path.join(DATOS_ESTADO, "otp_3ds_log.json"),
    "otp_3ds_autofill_config.json":   os.path.join(DATOS_ESTADO, "otp_3ds_autofill_config.json"),
    "otp_3ds_autofill_stats.json":    os.path.join(DATOS_ESTADO, "otp_3ds_autofill_stats.json"),
    "otp_3ds_webhook_inbox.json":     os.path.join(DATOS_ESTADO, "otp_3ds_webhook_inbox.json"),
    "polling_rotate_state.json":      os.path.join(DATOS_ESTADO, "polling_rotate_state.json"),
    "polling_rotate_log.json":        os.path.join(DATOS_ESTADO, "polling_rotate_log.json"),
    "presale_codes.json":             os.path.join(DATOS_ESTADO, "presale_codes.json"),
    "presale_farming_config.json":    os.path.join(DATOS_ESTADO, "presale_farming_config.json"),
    "proxy_sticky_state.json":        os.path.join(DATOS_ESTADO, "proxy_sticky_state.json"),
    "proxy_sticky_iplog.json":        os.path.join(DATOS_ESTADO, "proxy_sticky_iplog.json"),
    "queue_monitor_state.json":       os.path.join(DATOS_ESTADO, "queue_monitor_state.json"),
    "research_audit.json":            os.path.join(DATOS_ESTADO, "research_audit.json"),
    "session_persistence_log.json":   os.path.join(DATOS_ESTADO, "session_persistence_log.json"),
    "http2_frame_stats.json":         os.path.join(DATOS_ESTADO, "http2_frame_stats.json"),
    "http2_frame_config.json":        os.path.join(DATOS_ESTADO, "http2_frame_config.json"),
    "akamai_sbsd_log.json":           os.path.join(DATOS_ESTADO, "akamai_sbsd_log.json"),
    "proxies.txt":                    os.path.join(DATOS_ESTADO, "proxies.txt"),
    "_instance_id.txt":               os.path.join(DATOS_ESTADO, "_instance_id.txt"),
    "kv_store.db":                    os.path.join(DATOS_ESTADO, "kv_store.db"),
}

LOGS = {
    "bot.log":       os.path.join(LOGS_DIR, "bot.log"),
    "dashboard.log": os.path.join(LOGS_DIR, "dashboard.log"),
    "queue.log":     os.path.join(LOGS_DIR, "queue.log"),
}

def ruta(filename: str) -> str:
    """Resuelve nombre de archivo a ruta absoluta.

    Si TM_DATA_DIR está seteado, redirige a ese directorio (staging support).
    Esto se aplica solo a JSONs/db relativos al BASE_DIR — los logs y rutas
    absolutas en RUTAS/LOGS NO se afectan (son configuración explícita).
    """
    if filename in RUTAS:
        return RUTAS[filename]
    if filename in LOGS:
        return LOGS[filename]
    if os.path.isabs(filename):
        return filename
    # TM_DATA_DIR override (staging environment)
    data_dir = os.environ.get("TM_DATA_DIR", "").strip()
    if data_dir:
        # Solo permitimos paths relativos al proyecto (no absolutos
        # ni con .. para evitar escape del sandbox).
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(BASE_DIR, data_dir)
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, filename)
    return os.path.join(BASE_DIR, filename)

# ==========================================
#  CONFIG GENERAL
# ==========================================
MASTER_KEY_FILE  = os.path.join(SECRETS_DIR, ".master_key")
TELEGRAM_TOKEN   = ""
DASHBOARD_PORT   = int(os.environ.get("TM_PORT", "5055"))
DASHBOARD_HOST   = os.environ.get("TM_HOST", "0.0.0.0")
TM_ENV           = os.environ.get("TM_ENV", "production")  # production | staging | dev
SECRET_KEY       = "tm-perros-secret-change-me"
MONITOR_INTERVAL = 30
APP_NAME         = "TM PERROS"
APP_VERSION      = "1.0.0"


def self_test() -> dict:
    """Valida que ruta() resuelve nombres de estado a datos/estado/."""
    try:
        for name in ("proxy_health.json", "proxies.txt", "kv_store.db", "_instance_id.txt"):
            p = ruta(name)
            assert os.path.isabs(p), name
            assert os.path.dirname(p) == DATOS_ESTADO, f"{name} -> {p}"
        return {"ok": True, "rutas": len(RUTAS)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
