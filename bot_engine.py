"""bot_engine.py — Motor principal del sniper bot de Ticketmaster.

i18n:
  Los logs del worker se almacenan localizados según `lang` que se
  guarda en cada Worker/Group cuando se crean. Por defecto 'es'.
  El frontend del Sniper Bot envía el lang actual al crear grupos.

Ejecución real:
  Worker._run() spawn `_compra_with_swap.py` como subprocess
  (el wrapper aplica routing legacy/flowcore + bot_block retry).
"""
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

import config
import json_atomic


# ─────────────────────────────────────────────────────────────────
#  Slot counter — asigna slots únicos a workers spawneados
# ─────────────────────────────────────────────────────────────────
_PROYECTO = os.path.dirname(os.path.abspath(__file__))
_SLOT_COUNTER_FILE = os.path.join(_PROYECTO, "_bot_engine_slot_counter.txt")
_SLOT_LOCK = threading.Lock()


def _next_slot(start: int = 100) -> int:
    """Devuelve un slot único, persistente entre runs del dashboard.

    Empieza en 100 para no chocar con slots 1-99 que el usuario suele
    usar manualmente desde CLI. Roll-over a 100 cuando llega a 999.
    """
    with _SLOT_LOCK:
        try:
            with open(_SLOT_COUNTER_FILE, encoding="utf-8") as f:
                n = int((f.read() or "").strip() or str(start - 1))
        except Exception:
            n = start - 1
        n = n + 1 if start <= n < 999 else start
        try:
            with open(_SLOT_COUNTER_FILE, "w", encoding="utf-8") as f:
                f.write(str(n))
        except Exception:
            pass
        return n


# ═══════════════════════════════════════════════════════════════
#  i18n — strings localizados (ES/EN paridad 100%)
# ═══════════════════════════════════════════════════════════════
_T = {
    "es": {
        "worker.start": "Worker {wid} iniciando | dominio={dom} | evento={ev} | qty={qty} | mode={mode}",
        "worker.start.offer": " | offer_index={oi}",
        "worker.mode_quick": "Modo selección: ⚡ rápido (Ver lo mejor disponible)",
        "worker.mode_seatmap": "Modo selección: 🗺 mapa de asientos",
        "worker.category_fallback": "Categoría fallback: {cat}",
        "worker.offer_selected": "Oferta TM seleccionada (offer_index): {oi}",
        "worker.max_prices": "💰 Precios máx por oferta: {pretty}",
        "worker.max_prices_raw": "💰 offer_max_prices: {raw}",
        "worker.target_sections": "🎯 Sectores objetivo ({n}): {list}{ellipsis}",
        "worker.strict_targets": "   strict_targets={strict} — {explain}",
        "worker.strict_only": "SOLO buscará en los sectores marcados",
        "worker.strict_fallback": "priorizará marcados, fallback a otros activos",
        "worker.venue_cached": "🏟 Recinto cacheado: {vid}",
        "worker.launching": "Lanzando navegador...",
        "worker.in_queue": "En Queue-it, esperando posición...",
        "worker.invoking_orchestrator": "Invocando bot_orchestrator.run_purchase_flow(...) (browser real)",
        "worker.orch_stage": "orchestrator stage={stage} ok={ok}",
        "worker.orch_next": "   next: {nxt}",
        "worker.orch_error": "orchestrator error: {err}",
        "worker.invoking_flow": "Invocando tm_purchase_flow.run_flow(...)",
        "worker.flow_result": "flow result: {res}",
        "worker.flow_signature_mismatch": "run_flow signature mismatch ({err}); skipping deep call",
        "worker.flow_error": "flow error: {err}",
        "worker.stopped": "Worker detenido por usuario",
    },
    "en": {
        "worker.start": "Worker {wid} starting | domain={dom} | event={ev} | qty={qty} | mode={mode}",
        "worker.start.offer": " | offer_index={oi}",
        "worker.mode_quick": "Selection mode: ⚡ quick (See best available)",
        "worker.mode_seatmap": "Selection mode: 🗺 seat map",
        "worker.category_fallback": "Fallback category: {cat}",
        "worker.offer_selected": "Selected TM offer (offer_index): {oi}",
        "worker.max_prices": "💰 Max prices per offer: {pretty}",
        "worker.max_prices_raw": "💰 offer_max_prices: {raw}",
        "worker.target_sections": "🎯 Target sections ({n}): {list}{ellipsis}",
        "worker.strict_targets": "   strict_targets={strict} — {explain}",
        "worker.strict_only": "ONLY searches in marked sections",
        "worker.strict_fallback": "prioritizes marked, falls back to other active",
        "worker.venue_cached": "🏟 Cached venue: {vid}",
        "worker.launching": "Launching browser...",
        "worker.in_queue": "In Queue-it, waiting for position...",
        "worker.invoking_orchestrator": "Invoking bot_orchestrator.run_purchase_flow(...) (real browser)",
        "worker.orch_stage": "orchestrator stage={stage} ok={ok}",
        "worker.orch_next": "   next: {nxt}",
        "worker.orch_error": "orchestrator error: {err}",
        "worker.invoking_flow": "Invoking tm_purchase_flow.run_flow(...)",
        "worker.flow_result": "flow result: {res}",
        "worker.flow_signature_mismatch": "run_flow signature mismatch ({err}); skipping deep call",
        "worker.flow_error": "flow error: {err}",
        "worker.stopped": "Worker stopped by user",
    },
}


def _t(key: str, lang: str = "es", **fmt) -> str:
    """Resuelve key en el lang dado. Fallback a ES si no existe en EN."""
    s = _T.get(lang, _T["es"]).get(key) or _T["es"].get(key, key)
    try:
        return s.format(**fmt) if fmt else s
    except Exception:
        return s


_ALLOWED_PARAM_KEYS = (
    "selection_mode",  # 'quick' | 'seatmap'
    "offer_index",     # int — índice de oferta detectada (modo legacy single-select)
    "selected_offer_indices",  # list[int] — multi-select en orden de prioridad. Default [0,1,2,...]
    "offer_max_prices",  # dict {offer_index: max_price} — bot rechaza asientos por encima del precio máximo
    "selected_categories",  # list[str] — categorías genéricas marcadas (floor/ga/reserved/vip/boxes/wheelchair) en orden de prioridad
    "category",        # str — categoría genérica fallback (floor/ga/reserved/vip/...)
    "quantity",        # int
    "url",             # str — URL de evento (si no se sacó de event_id)
    "browser",         # str — engine ('auto'|'morelogin'|'camoufox'|...)
    "captcha_provider",# str — 'auto'|'2captcha'|'capsolver'|'anticaptcha'
    "strategy",        # str
    "retries", "retry_delay_s", "queue_timeout_min", "cooldown_min",
    # Modo seatmap: el user marca sectores específicos del recinto cacheado
    "selected_sections",  # list[str] — section_ids ('s_27', 's_38', ...) marcados
    "venue_id",           # str — id del venue cacheado (multi-evento, p.ej. 'estadi-olimpic-...')
    "strict_targets",     # bool — solo sectores marcados (no caer en otros). Default True si selected_sections viene.
)


def _filter_params(d):
    if not isinstance(d, dict): return {}
    out = {}
    for k in _ALLOWED_PARAM_KEYS:
        if k in d and d[k] is not None and d[k] != "":
            out[k] = d[k]
    return out


def _account_blocked_for_event(account_id: str, event_id: str,
                                default_max: int = 6) -> bool:
    """Devuelve True si la cuenta ya compró >= max_qty entradas para este evento.
    Lee `data/event_purchase_limits.json` para max custom y `purchases_manager` para histórico.
    Cruza por account_id Y por account_email (tolerante a purchases legacy)."""
    try:
        import os, json as _json
        import account_manager
        import purchases_manager
        # Resolver email de la cuenta
        accs = account_manager.account_manager.list_accounts()
        a = next((x for x in accs if x.get("id") == account_id), None)
        email = (a or {}).get("tm_email", "").lower()
        # Leer límite por evento
        limits_path = os.path.join(os.path.dirname(__file__), "data", "event_purchase_limits.json")
        try:
            with open(limits_path, encoding="utf-8") as f: limits = _json.load(f)
        except Exception: limits = {}
        max_qty = int(limits.get(str(event_id), default_max))
        # Sumar quantities de purchases vinculadas
        total = 0
        for p in purchases_manager.list_purchases():
            if str(p.get("event_id")) != str(event_id) and str(p.get("tm_id")) != str(event_id):
                continue
            same_id = bool(account_id) and p.get("account_id") == account_id
            same_email = bool(email) and (p.get("account_email") or "").lower() == email
            if same_id or same_email:
                total += int(p.get("quantity") or 0)
        return total >= max_qty
    except Exception:
        return False


class Worker:
    def __init__(self, group_id, event_id, domain_id, account_id=None, params=None, lang="es"):
        self.id = uuid.uuid4().hex[:8]
        self.group_id = group_id
        self.event_id = event_id
        self.domain_id = domain_id
        self.account_id = account_id
        self.status = "idle"  # idle | launching | in_queue | ready | purchasing | success | error
        self.logs = []
        self.started_at = None
        self._thread = None
        self.lang = lang if lang in ("es", "en") else "es"
        self.params = _filter_params(params or {})  # selection_mode, offer_index, qty, etc.

    def log(self, msg, level="info"):
        self.logs.append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        })
        self.logs = self.logs[-200:]

    def start(self):
        self.status = "launching"
        self.started_at = datetime.now().isoformat()
        sel_mode = self.params.get("selection_mode", "quick")
        qty = self.params.get("quantity")
        offer_idx = self.params.get("offer_index")
        line = _t("worker.start", self.lang, wid=self.id, dom=self.domain_id,
                   ev=self.event_id, qty=qty, mode=sel_mode)
        if offer_idx is not None:
            line += _t("worker.start.offer", self.lang, oi=offer_idx)
        self.log(line)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        """Spawn `_compra_with_swap.py` con los params del worker.

        El wrapper aplica routing legacy/flowcore + bot_block retry.
        Tras terminar, lee outcome.json y mapea el resultado al
        worker.status para que la UI lo muestre.
        """
        sel_mode = self.params.get("selection_mode", "quick")
        self.log(_t("worker.mode_quick" if sel_mode == "quick" else "worker.mode_seatmap", self.lang))
        if self.params.get("category"):
            self.log(_t("worker.category_fallback", self.lang, cat=self.params["category"]))
        if self.params.get("offer_index") is not None:
            self.log(_t("worker.offer_selected", self.lang, oi=self.params["offer_index"]))
        # Loggear precios máximos si vienen
        omp = self.params.get("offer_max_prices") or {}
        if omp:
            try:
                pretty = ', '.join(f'#{k}≤{v}€' for k, v in omp.items())
                self.log(_t("worker.max_prices", self.lang, pretty=pretty))
            except Exception:
                self.log(_t("worker.max_prices_raw", self.lang, raw=omp))
        secs = self.params.get("selected_sections") or []
        if sel_mode == "seatmap" and secs:
            strict = self.params.get("strict_targets")
            if strict is None: strict = True
            ell = '...' if len(secs) > 8 else ''
            self.log(_t("worker.target_sections", self.lang, n=len(secs),
                         **{"list": ', '.join(secs[:8]), "ellipsis": ell}))
            explain = _t("worker.strict_only" if strict else "worker.strict_fallback", self.lang)
            self.log(_t("worker.strict_targets", self.lang, strict=strict, explain=explain))
        if self.params.get("venue_id"):
            self.log(_t("worker.venue_cached", self.lang, vid=self.params["venue_id"]))

        # ─── Resolver event_url ────────────────────────────────────
        event_url = (self.params.get("url") or "").strip()
        if not event_url and self.event_id:
            try:
                import event_manager
                for e in event_manager.list_events():
                    if (str(e.get("id")) == str(self.event_id)
                            or str(e.get("tm_id")) == str(self.event_id)):
                        event_url = e.get("tm_url") or e.get("url") or ""
                        break
            except Exception:
                pass
        if not event_url:
            self.status = "error"
            self.log("ERROR: no event_url disponible (set params.url o event_id mapeable)", level="error")
            return

        # ─── Resolver engine (browser) ──────────────────────────────
        # MoreLogin no pasa Akamai en TM España (bmak.js no sirve).
        # Camoufox sí (Firefox C++ patched, WebRTC block, stealth nativo).
        # Memoria 2026-05-12 confirma camoufox funciona end-to-end.
        # Mapeo:
        #   params.browser=morelogin → --engine morelogin
        #   params.browser=camoufox  → --engine camoufox
        #   params.browser=auto/None → --engine camoufox (default operativo)
        browser_raw = (self.params.get("browser") or "").strip().lower()
        if browser_raw == "morelogin":
            engine = "morelogin"
        else:
            engine = "camoufox"   # auto + camoufox + cualquier otra cosa
        self.log(f"engine seleccionado: {engine} "
                  f"(browser param='{browser_raw or 'auto'}')")

        # ─── Construir argv para _compra_with_swap.py ───────────────
        slot = _next_slot()
        wrapper = os.path.join(_PROYECTO, "_compra_with_swap.py")
        argv = [
            sys.executable, wrapper,
            "--slot", str(slot),
            "--event-url", event_url,
            "--qty", str(self.params.get("quantity") or 1),
            "--mode", sel_mode,
            "--engine", engine,
        ]
        if self.params.get("offer_index") is not None:
            argv += ["--offer-idx", str(self.params["offer_index"])]
        if secs:
            argv += ["--sections", ",".join(str(s) for s in secs)]
        if self.params.get("allow_separated"):
            argv.append("--allow-separated")
        # --confirm queda DESHABILITADO por defecto: pruebas sin click final
        # en "Pagar". Para compra real, añade --confirm vía CLI o param.
        if self.params.get("confirm_buy"):
            argv.append("--confirm")

        # ─── Spawn subprocess ───────────────────────────────────────
        log_path = os.path.join(_PROYECTO, f"_compra_worker_{self.id}.log")
        self.log(_t("worker.launching", self.lang))
        self.log(f"spawn _compra_with_swap.py · slot={slot} · "
                  f"qty={self.params.get('quantity') or 1} · "
                  f"mode={sel_mode} · engine={engine} · "
                  f"log=_compra_worker_{self.id}.log")
        try:
            logf = open(log_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                argv, cwd=_PROYECTO,
                stdout=logf, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            self._proc = proc  # para que stop() pueda terminate()
            self.status = "purchasing"
            self.log(f"subprocess started · pid={proc.pid}")
        except Exception as e:
            self.status = "error"
            self.log(f"spawn_error: {type(e).__name__}: {e}", level="error")
            try: logf.close()
            except Exception: pass
            return

        # ─── Wait + outcome ─────────────────────────────────────────
        try:
            rc = proc.wait()
        except Exception as e:
            self.status = "error"
            self.log(f"wait_error: {type(e).__name__}: {e}", level="error")
            return
        finally:
            try: logf.close()
            except Exception: pass

        outcome_path = os.path.join(_PROYECTO,
                                      f"_compra_parallel_slot{slot}.outcome.json")
        outcome = {}
        try:
            with open(outcome_path, encoding="utf-8") as f:
                outcome = json.load(f)
        except Exception:
            pass

        status = outcome.get("status", "unknown")
        step = outcome.get("step_reached", "?")
        self.log(f"subprocess finished · rc={rc} · status={status} · step={step}")

        # Map outcome → worker.status
        if rc == 0 and status in ("checkout_ok", "purchase_completed", "success", "ok"):
            self.status = "success"
        elif rc == 99 or status in ("bot_block", "kill_switch_aborted",
                                       "fatal_exception"):
            self.status = "error"
        else:
            self.status = "error"

    def stop(self):
        # Si hay subprocess vivo, terminar de forma controlada
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                self.log(f"subprocess terminate · pid={proc.pid}")
            except Exception as e:
                self.log(f"stop_error: {type(e).__name__}: {e}", level="error")
        self.status = "stopped"
        self.log(_t("worker.stopped", self.lang))

    def to_dict(self):
        return {
            "id": self.id,
            "group_id": self.group_id,
            "event_id": self.event_id,
            "domain_id": self.domain_id,
            "account_id": self.account_id,
            "status": self.status,
            "started_at": self.started_at,
            "params": self.params,
            "logs": self.logs[-30:],
        }


class Group:
    def __init__(self, event_id, domain_ids, name="", params=None, lang="es"):
        self.id = uuid.uuid4().hex[:8]
        self.event_id = event_id
        self.domain_ids = domain_ids if isinstance(domain_ids, list) else [domain_ids]
        self.name = name or f"Group {self.id}"
        self.workers = []
        self.strategy = (params or {}).get("strategy") or "equal"
        self.status = "idle"
        self.created_at = datetime.now().isoformat()
        self.lang = lang if lang in ("es", "en") else "es"
        self.params = _filter_params(params or {})  # heredables a workers nuevos

    def add_worker(self, account_id=None, params=None):
        dom = self.domain_ids[len(self.workers) % len(self.domain_ids)]
        # merge: params específicos del worker overrride a params del grupo
        merged = {**self.params, **(_filter_params(params or {}))}
        # Auto-skip cuentas que ya alcanzaron el límite de compras para este evento
        # (lee event_purchase_limits.json + purchases_manager.list_purchases).
        if account_id and self.event_id:
            try:
                if _account_blocked_for_event(account_id, self.event_id):
                    # NO crear worker — devolver None y dejar que el caller intente otra cuenta
                    return None
            except Exception:
                pass
        w = Worker(self.id, self.event_id, dom, account_id, params=merged, lang=self.lang)
        self.workers.append(w)
        return w

    def start_all(self):
        self.status = "running"
        for w in self.workers:
            w.start()

    def stop_all(self):
        self.status = "stopped"
        for w in self.workers:
            w.stop()

    def to_dict(self):
        return {
            "id": self.id,
            "event_id": self.event_id,
            "domain_ids": self.domain_ids,
            "name": self.name,
            "strategy": self.strategy,
            "status": self.status,
            "created_at": self.created_at,
            "params": self.params,
            "workers": [w.to_dict() for w in self.workers],
        }


class BotEngine:
    def __init__(self):
        self.groups = {}

    def create_group(self, event_id, domain_ids, name="", params=None, lang="es"):
        g = Group(event_id, domain_ids, name, params=params, lang=lang)
        self.groups[g.id] = g
        self._persist()
        return g

    def list_groups(self):
        return [g.to_dict() for g in self.groups.values()]

    def get_group(self, gid):
        return self.groups.get(gid)

    def delete_group(self, gid):
        if gid in self.groups:
            self.groups[gid].stop_all()
            del self.groups[gid]
            self._persist()
            return True
        return False

    def _persist(self):
        json_atomic.save(
            config.ruta("bot_groups.json"),
            {"groups": [g.to_dict() for g in self.groups.values()]},
        )


engine = BotEngine()


def self_test() -> dict:
    """Validación del módulo + paridad i18n ES/EN."""
    out = {"ok": True, "checks": {}, "errors": []}
    try:
        # 1. Engine instanciable
        out["checks"]["engine_present"] = engine is not None
        out["checks"]["filter_params_callable"] = callable(_filter_params)

        # 2. i18n parity
        es_keys = set(_T.get("es", {}).keys())
        en_keys = set(_T.get("en", {}).keys())
        only_es = es_keys - en_keys
        only_en = en_keys - es_keys
        out["checks"]["i18n_es_keys"] = len(es_keys)
        out["checks"]["i18n_en_keys"] = len(en_keys)
        out["checks"]["i18n_parity"] = (len(only_es) == 0 and len(only_en) == 0)
        if only_es:
            out["errors"].append(f"only ES: {sorted(only_es)}")
        if only_en:
            out["errors"].append(f"only EN: {sorted(only_en)}")

        # 3. _t resolver funciona en ambos lang
        s_es = _t("worker.launching", "es")
        s_en = _t("worker.launching", "en")
        out["checks"]["t_es_present"] = bool(s_es) and "navegador" in s_es
        out["checks"]["t_en_present"] = bool(s_en) and "browser" in s_en.lower()
        out["checks"]["t_diff"] = s_es != s_en

        # 4. Worker acepta lang
        w_en = Worker("g1", "e1", "es", lang="en")
        out["checks"]["worker_lang_en"] = w_en.lang == "en"
        w_es = Worker("g1", "e1", "es")  # default
        out["checks"]["worker_lang_default"] = w_es.lang == "es"

        for k, v in out["checks"].items():
            if v is False:
                out["ok"] = False
                out["errors"].append(f"check failed: {k}")
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"{type(e).__name__}: {e}")
    return out
