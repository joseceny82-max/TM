"""bot_engine.py — Motor principal del sniper bot de Ticketmaster."""
import uuid
import threading
from datetime import datetime
import config
import json_atomic


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
    def __init__(self, group_id, event_id, domain_id, account_id=None, params=None):
        self.id = uuid.uuid4().hex[:8]
        self.group_id = group_id
        self.event_id = event_id
        self.domain_id = domain_id
        self.account_id = account_id
        self.status = "idle"  # idle | launching | in_queue | ready | purchasing | success | error
        self.logs = []
        self.started_at = None
        self._thread = None
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
        self.log(
            f"Worker {self.id} iniciando | dominio={self.domain_id} | evento={self.event_id} "
            f"| qty={qty} | mode={sel_mode}"
            + (f" | offer_index={offer_idx}" if offer_idx is not None else "")
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        # Stub orquestador. El executor real (tm_purchase_flow / ticket_selector) se conecta aquí.
        # Por ahora deja constancia clara de que el payload ha viajado correctamente.
        sel_mode = self.params.get("selection_mode", "quick")
        self.log(f"Modo selección: {'⚡ rápido (Ver lo mejor disponible)' if sel_mode=='quick' else '🗺 mapa de asientos'}")
        if self.params.get("category"):
            self.log(f"Categoría fallback: {self.params['category']}")
        if self.params.get("offer_index") is not None:
            self.log(f"Oferta TM seleccionada (offer_index): {self.params['offer_index']}")
        # Loggear precios máximos si vienen
        omp = self.params.get("offer_max_prices") or {}
        if omp:
            try:
                pretty = ', '.join(f'#{k}≤{v}€' for k, v in omp.items())
                self.log(f"💰 Precios máx por oferta: {pretty}")
            except Exception:
                self.log(f"💰 offer_max_prices: {omp}")
        secs = self.params.get("selected_sections") or []
        if sel_mode == "seatmap" and secs:
            # Default: si el usuario marcó sectores, modo estricto (solo esos).
            strict = self.params.get("strict_targets")
            if strict is None: strict = True
            self.log(f"🎯 Sectores objetivo ({len(secs)}): {', '.join(secs[:8])}{'...' if len(secs) > 8 else ''}")
            self.log(f"   strict_targets={strict} — {'SOLO buscará en los sectores marcados' if strict else 'priorizará marcados, fallback a otros activos'}")
        if self.params.get("venue_id"):
            self.log(f"🏟 Recinto cacheado: {self.params['venue_id']}")
        self.log("Lanzando navegador...")
        self.status = "in_queue"
        self.log("En Queue-it, esperando posicion...")

        # Derivar prefer_offer_indices / selected_sections / strict_targets
        # ANTES de usarlas en cualquier orquestador (fix F821).
        _secs = self.params.get("selected_sections") or []
        _strict = self.params.get("strict_targets")
        if _strict is None and _secs:
            _strict = True
        _multi = self.params.get("selected_offer_indices") or []
        _oi = self.params.get("offer_index")
        if _multi:
            _prefer = [int(x) for x in _multi]
        elif _oi is not None:
            _prefer = [int(_oi)]
        else:
            _prefer = None

        # Hook real BROWSER-BASED: orquestador integrado venue/ticket/cart/queue.
        try:
            import bot_orchestrator
            run_kwargs_browser = {
                "event_id": self.event_id,
                "url": self.params.get("url"),
                "quantity": self.params.get("quantity") or 1,
                "selection_mode": sel_mode,
                "prefer_offer_indices": _prefer,
                "selected_sections": _secs or None,
                "strict_targets": _strict,
                "venue_id": self.params.get("venue_id"),
                "offer_max_prices": self.params.get("offer_max_prices"),
                "account_id": self.account_id,
                "captcha_provider": self.params.get("captcha_provider") or "auto",
                "browser": self.params.get("browser") or "auto",
                "worker_id": self.id,
            }
            self.log("Invocando bot_orchestrator.run_purchase_flow(...) (browser real)")
            try:
                res = bot_orchestrator.run_purchase_flow(**run_kwargs_browser)
                self.log(f"orchestrator stage={res.get('stage')} ok={res.get('ok')}")
                if res.get("next_action"):
                    self.log(f"   next: {res['next_action'][:160]}")
                self.status = "success" if res.get("ok") else "error"
            except Exception as e:
                self.log(f"orchestrator error: {e}", level="error")
                self.status = "error"
        except ImportError:
            pass

        # Hook fallback simulate: si bot_orchestrator no disponible o falla, prueba flow simulate.
        try:
            import tm_purchase_flow
            # _prefer / _secs / _strict ya derivados arriba (línea ~125)
            run_kwargs = {
                "event_id": self.event_id,
                "url": self.params.get("url"),
                "quantity": self.params.get("quantity") or 1,
                "category": self.params.get("category"),
                "selection_mode": sel_mode,
                "offer_index": _oi,
                "selected_offer_indices": _multi or None,
                "prefer_offer_indices": _prefer,  # NUEVO: para select_quick_smart
                "offer_max_prices": self.params.get("offer_max_prices"),
                "selected_sections": _secs or None,
                "strict_targets": _strict,
                "venue_id": self.params.get("venue_id"),
                "domain_id": self.domain_id,
                "account_id": self.account_id,
                "browser": self.params.get("browser") or "auto",
                "captcha_provider": self.params.get("captcha_provider") or "auto",
                "worker_id": self.id,
            }
            if hasattr(tm_purchase_flow, "run_flow"):
                self.log("Invocando tm_purchase_flow.run_flow(...)")
                # Best-effort: si falla, queda registrado pero el worker no crashea.
                try:
                    res = tm_purchase_flow.run_flow(**run_kwargs)
                    self.log(f"flow result: {str(res)[:200]}")
                except TypeError as e:
                    # run_flow no acepta esos kwargs todavía → fallback a sus args originales
                    self.log(f"run_flow signature mismatch ({e}); skipping deep call")
                except Exception as e:
                    self.log(f"flow error: {e}", level="error")
        except ImportError:
            pass

    def stop(self):
        self.status = "stopped"
        self.log("Worker detenido por usuario")

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
    def __init__(self, event_id, domain_ids, name="", params=None):
        self.id = uuid.uuid4().hex[:8]
        self.event_id = event_id
        self.domain_ids = domain_ids if isinstance(domain_ids, list) else [domain_ids]
        self.name = name or f"Group {self.id}"
        self.workers = []
        self.strategy = (params or {}).get("strategy") or "equal"
        self.status = "idle"
        self.created_at = datetime.now().isoformat()
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
        w = Worker(self.id, self.event_id, dom, account_id, params=merged)
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

    def create_group(self, event_id, domain_ids, name="", params=None):
        g = Group(event_id, domain_ids, name, params=params)
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
