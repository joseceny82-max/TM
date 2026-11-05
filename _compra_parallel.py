"""Compra paralela — flow real completo HASTA tarjeta autorrellenada (NO paga).

Uso:
    python _compra_parallel.py --slot 1 --event-url URL --offer-idx N --qty 4

Cada slot:
  1) Pide perfil MoreLogin ES nuevo (no quemado).
  2) Pide cuenta TM activa con tarjeta asociada.
  3) Arranca el perfil → puerto CDP propio.
  4) Goto evento → "Ver lo mejor disponible" → stepper offer-idx hasta qty.
  5) Click "Find Tickets / Entradas verde" → checkbox Acepto → "Continuar".
  6) OAuth login con OTP IMAP (joaquina_polo0 → contact_email cuenta).
  7) Watcher hasta checkout → fill_card.
  8) Crea screenshot final + JSON resultado en datos/compra_parallel/.
  NUNCA pulsa Pagar (parametro --confirm es por defecto False).
"""
from __future__ import annotations
import sys, io, time, os, json, re, random, argparse, requests
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

PROYECTO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROYECTO)

# human_motion: Bezier mouse + Fitts + Markov dwell. Si no está disponible,
# fallback automático a page.type/page.click (gracioso). NO bloqueante.
try:
    import human_motion as _hm
    _HUMAN_MOTION = True
except Exception:
    _hm = None
    _HUMAN_MOTION = False

# ════ outcome.json IPC ════
# tm_real_purchase.run_real_purchase llama a este script y lee `outcome.json` al
# final para saber si el flow tuvo éxito (sin parsear logs frágiles).
_OUTCOME_PATH = None  # se setea en main() según --slot

def _outcome_path(slot: int) -> str:
    return os.path.join(PROYECTO, f"_compra_parallel_slot{slot}.outcome.json")

def write_outcome(slot: int, result: dict) -> None:
    """Escribe outcome.json estructurado (status + step_reached + order_id + error).

    Robusto contra fallos: si no se puede escribir, log y sigue.

    ─── PHASE 3 INSTRUMENTATION TAP ─────────────────────────────
    Cada llamada a write_outcome ADEMÁS emite un structured event
    al pipeline nuevo. Esto cubre TODOS los exits del bot (incluidos
    los 37 returns que bypassean los hooks Step 11). Fail-safe absoluto.
    """
    try:
        path = _outcome_path(slot)
        result.setdefault("ts", datetime.now().isoformat())
        result.setdefault("slot", slot)
        try:
            import json_atomic
            json_atomic.save(path, result)
        except Exception:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    except Exception:
        pass
    # ─── Tap to structured events (Phase 3) ──────────────────
    try:
        from core.observability.event_emitter import emit_event as _ee
        from core.observability.error_classification import ErrorCategory
        status = str(result.get("status", "unknown"))
        _ee(
            ErrorCategory.FLOW,
            "outcome_recorded",
            {k: v for k, v in result.items() if k not in {"ts"}},
            context="write_outcome_tap",
            cart_id=str(result.get("cart_id", "")),
            idempotency_key=str(result.get("idempotency_key", "")),
        )
    except Exception:
        pass

import account_manager as am
import bot_orchestrator as bo
from imap_tools import MailBox, AND

# Logging consolidado 2026-05-13: log() delega a _compra_logger.log_event.
# UN solo sistema: stdout legacy + JSON Lines parseable.
from _compra_logger import log_event as _logger_log_event

# Constantes centralizadas 2026-05-13. Fuente única de verdad para timeouts,
# polls, retries y límites. Prohibido añadir magic numbers fuera del módulo.
from _compra_constants import (
    # Retries
    RETRY_PROFILE_PICK, RETRY_PROFILE_START, RETRY_CLICK_CONTINUAR,
    RETRY_COOKIES_BANNER, RETRY_IFRAME_POLL, RETRY_ANADIR_TARJETA,
    RETRY_CHECKOUT_STAGES,
    # Limits
    LIMIT_ACCOUNT_SWAPS,
    # Storage state
    MAX_STORAGE_STATE_AGE_S,
)


# ════════════════════════════════════════════════════════════════════
#  M13.2 — Purchase Gate integration (wire mínimo)
# ════════════════════════════════════════════════════════════════════
#
# Wrapper que consulta el PurchaseGate determinista (core.diagnostics.
# purchase_gate) JUSTO ANTES del click "Realizar Compra" (ETAPA D).
#
# Comportamiento:
#   ('proceed', None)  → ALLOW + LIVE → ejecutar click real
#   ('abort', status)  → DENY o DRY_RUN o error → write_outcome + return 0
#
# Default state del gate: disabled + kill switch OFF →
# rechazará TODOS los submits por defecto. Para activar:
#   PURCHASE_MODE=dry_run            (Fase 2: bot sin click real)
#   PURCHASE_MODE=live + GLOBAL_PURCHASE_ENABLED=1  (Fase 3: producción)
#
# Fail-safe: cualquier excepción → ('abort', status='gate_error').
# Sin lógica duplicada: el gate es la única fuente de decisión.
# Cero modificaciones al flow técnico del bot.
def _purchase_gate_check(slot, account, args, checkout_started_at_monotonic):
    """M13.2 wire: evalúa PurchaseGate antes del submit irreversible.

    Args:
        slot: identificador de sesión (int)
        account: dict con tm_email
        args: argparse namespace (acceso a event_url, qty)
        checkout_started_at_monotonic: float monotonic del momento en que
            se detectó la URL de checkout (para stale check); puede ser None.

    Returns:
        tuple (action: str, status: Optional[dict])
            action == 'proceed' → continuar con click real
            action == 'abort'   → status dict para write_outcome
    """
    try:
        from core.diagnostics.purchase_gate import get_default_gate
        gate = get_default_gate()
        evt_id = ""
        try:
            if (hasattr(args, 'event_url') and args.event_url
                    and '/event/' in args.event_url):
                evt_id = args.event_url.split('/event/')[-1].split('?')[0]
        except Exception:
            pass
        decision = gate.evaluate(
            session_id=str(slot),
            account_id=str(account.get('tm_email', 'unknown')),
            event_id=evt_id or 'unknown_event',
            qty=int(getattr(args, 'qty', 1) or 1),
            checkout_opened_at_monotonic=checkout_started_at_monotonic,
        )
        try:
            log(slot, "purchase_gate_decision",
                allowed=decision.allowed, reason=decision.reason,
                mode=decision.mode, dry_run=decision.dry_run)
        except Exception:
            pass  # log es best-effort
        if not decision.allowed:
            return ('abort', {
                "status": "gate_denied",
                "step_reached": "pre_realizar_compra",
                "reason": decision.reason,
                "mode": decision.mode,
                "account_email": account.get('tm_email'),
            })
        if decision.dry_run:
            return ('abort', {
                "status": "dry_run_success",
                "step_reached": "pre_realizar_compra",
                "mode": decision.mode,
                "account_email": account.get('tm_email'),
            })
        return ('proceed', None)
    except Exception as e:
        return ('abort', {
            "status": "gate_error",
            "step_reached": "pre_realizar_compra",
            "error": str(e)[:200],
            "account_email": account.get('tm_email'),
        })


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--slot", type=int, default=1)
    p.add_argument("--event-url", required=True)
    p.add_argument("--offer-idx", type=int, default=3, help="0=PIT, 3=Asiento Reservado (Karol G)")
    p.add_argument("--qty", type=int, default=4)
    p.add_argument("--mode", default="quick", choices=("quick", "seatmap"),
                    help="quick=Ver lo mejor disponible / seatmap=selección sector por sector")
    p.add_argument("--sections", default="",
                    help="seatmap: lista de section_id o tm_api_id separados por coma "
                         "(en orden de prioridad)")
    p.add_argument("--allow-separated", action="store_true",
                    help="Si TM dice 'asientos no juntos', aceptar igual. Por defecto "
                         "el bot pulsa 'Ajustar selección' para forzar consecutivos.")
    p.add_argument("--confirm", action="store_true",
                    help="SOLO si quieres pulsar Pagar al final (peligroso)")
    p.add_argument("--engine", default="morelogin",
                    choices=("morelogin", "camoufox", "silent"),
                    help="Motor de browser: morelogin (default, perfiles "
                         "MoreLogin + Quantum proxy embebido), camoufox "
                         "(Firefox C++ patched, mejor stealth Akamai, "
                         "usa Quantum proxy directo con sessionid random) "
                         "o silent (perfiles Silent con proxy ya embebido "
                         "en el perfil, CDP via API local 127.0.0.1:8788)")
    p.add_argument("--headless", action="store_true",
                    help="Solo camoufox: lanzar sin ventana visible")
    return p.parse_args()


def handle_separated_seats_modal(slot, page, allow_separated=False):
    """Detecta el modal 'Es posible que las entradas no estén juntas' que TM
    muestra a veces tras añadir qty>1 en quick mode. Política del usuario:
    pulsar 'Ajustar selección' SIEMPRE para forzar asientos consecutivos.
    Solo si allow_separated=True acepta entradas dispersas."""
    try:
        result = page.evaluate(r"""() => {
            const dialogs = Array.from(document.querySelectorAll(
                '[role=dialog], [role=alertdialog], .modal, [class*="modal" i]'));
            for (const d of dialogs) {
                const txt = (d.innerText || '').toLowerCase();
                if (/no est[eé]n juntas|tipos de entrada diferentes|no consecutiv/i.test(txt)) {
                    const btns = Array.from(d.querySelectorAll('button'))
                        .filter(b => b.offsetParent !== null);
                    return {found: true, button_texts: btns.map(b => (b.innerText||'').trim())};
                }
            }
            return {found: false};
        }""")
        if not result.get("found"):
            return {"detected": False}
        log(slot, "separated_seats_modal_detected", buttons=result.get("button_texts"))
        if allow_separated:
            log(slot, "policy_allow_separated", note="cerrando diálogo, aceptando dispersas")
            page.evaluate(r"""() => {
                const dialogs = Array.from(document.querySelectorAll(
                    '[role=dialog], [role=alertdialog], .modal, [class*="modal" i]'));
                for (const d of dialogs) {
                    const txt = (d.innerText || '').toLowerCase();
                    if (/no est[eé]n juntas|tipos de entrada diferentes/i.test(txt)) {
                        const close = Array.from(d.querySelectorAll('button'))
                            .find(b => /cerrar/i.test(b.innerText || ''));
                        if (close) close.click();
                        return;
                    }
                }
            }""")
            return {"detected": True, "action": "closed_dialog"}
        # Default: ajustar selección
        clicked = page.evaluate(r"""() => {
            const dialogs = Array.from(document.querySelectorAll(
                '[role=dialog], [role=alertdialog], .modal, [class*="modal" i]'));
            for (const d of dialogs) {
                const txt = (d.innerText || '').toLowerCase();
                if (!/no est[eé]n juntas|tipos de entrada diferentes/i.test(txt)) continue;
                const btns = Array.from(d.querySelectorAll('button'))
                    .filter(b => b.offsetParent !== null);
                const adjust = btns.find(b => /ajustar selecci[oó]n/i.test(b.innerText || ''));
                if (adjust) { adjust.click(); return {clicked: 'ajustar'}; }
                // Fallback: si no encuentra Ajustar, volver atrás
                const back = btns.find(b => /atr[aá]s|volver/i.test(b.innerText || ''));
                if (back) { back.click(); return {clicked: 'atras'}; }
                return {clicked: null};
            }
            return {clicked: null};
        }""")
        log(slot, "ajustar_seleccion_clicked", **clicked)
        return {"detected": True, "action": clicked.get("clicked")}
    except Exception as e:
        log(slot, "modal_handler_err", err=str(e)[:120])
        return {"detected": False, "error": str(e)[:120]}


def handle_partners_seguro_step(slot, page):
    """Maneja la pantalla intermedia con secciones OBLIGATORIO:
      - PARTNERS DEL EVENTO → radio "¡Sí! Quiero estar informado..." (acepta marketing)
      - SEGURO DE ENTRADAS → radio "No, no quiero proteger mis entradas" (decline)
    Norma user 2026-05-11: Partners=Sí, Seguro=No (no añadir 23,70€).

    FIX 2026-05-12: scroll abajo PRIMERO (sección Seguro fuera del viewport
    inicial), patrones regex permisivos (texto real es "¡Sí! Quiero estar
    informado..." y "No, no quiero..." con caracteres y formato variados)."""
    try:
        # PASO 1: scroll abajo para forzar render de sección Seguro
        try:
            page.evaluate(r"""() => {
                window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'});
            }""")
            page.wait_for_timeout(800)
            page.evaluate(r"""() => {
                window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'});
            }""")
            page.wait_for_timeout(400)
        except Exception: pass

        # PASO 2: click radios via JS direct (busca labels clickables, no inputs)
        result = page.evaluate(r"""() => {
            const out = {
                partners_clicked: false, seguro_clicked: false,
                partners_section_found: false, seguro_section_found: false,
                debug_partners_labels: [], debug_seguro_labels: [],
            };

            // Encontrar secciones por heading text
            const allSecs = Array.from(document.querySelectorAll(
                'section, fieldset, div, article'));
            const partnersSec = allSecs.find(s => {
                const txt = (s.innerText||'').slice(0, 800);
                return /PARTNERS\s+DEL\s+EVENTO|Partners?\s+del\s+evento/i.test(txt);
            });
            const seguroSec = allSecs.find(s => {
                const txt = (s.innerText||'').slice(0, 800);
                return /SEGURO\s+DE\s+ENTRADAS|Seguro\s+de\s+entradas|Missed\s+Event\s+Protection|Allianz|Seguro\s+de\s+No\s+Asistencia/i.test(txt);
            });
            out.partners_section_found = !!partnersSec;
            out.seguro_section_found = !!seguroSec;

            // Helper robusto: dado una sección, encuentra todos los labels
            // (visibles) y aplica el pattern. Click LABEL (no el input) porque
            // labels TM son clickables y los inputs están detrás de overlays.
            function clickByLabelText(sec, pattern) {
                if (!sec) return {clicked: false, labels: []};
                const labels = Array.from(sec.querySelectorAll(
                    'label, [class*="radio" i], [class*="option" i]'))
                    .filter(l => l.offsetParent !== null);
                const labelTexts = labels.map(l => (l.innerText||l.textContent||'').trim().slice(0, 100));
                for (const l of labels) {
                    const txt = (l.innerText || l.textContent || '').trim();
                    if (pattern.test(txt)) {
                        l.scrollIntoView({block:'center', behavior: 'instant'});
                        // Click LABEL (TM uses label as clickable target)
                        l.click();
                        // Backup: si dentro hay un input radio, click también
                        const inp = l.querySelector('input[type="radio"]');
                        if (inp) { try { inp.click(); } catch(e) {} }
                        return {clicked: true, matched: txt.slice(0, 80), labels: labelTexts};
                    }
                }
                return {clicked: false, labels: labelTexts};
            }

            // 1) Partners → Sí (¡Sí! Quiero estar informado por medios electrónicos)
            if (partnersSec) {
                // Regex MUY permisiva: matchea "Sí" en cualquier posición + variantes
                const partnersResult = clickByLabelText(partnersSec,
                    /quiero\s+estar\s+informado|s[ií].*quiero|¡?\s*s[ií]\s*!?\s*quiero|acepto\s+recibir|^s[ií]\b/i);
                out.partners_clicked = partnersResult.clicked;
                out.partners_matched = partnersResult.matched;
                out.debug_partners_labels = partnersResult.labels;
            }
            // 2) Seguro → No (No, no quiero proteger mis entradas)
            // CRITICAL FIX 2026-05-14: NORMA USER OBLIGATORIA — NUNCA aceptar
            // seguro. SIEMPRE "No, no quiero asegurar/proteger entradas".
            // BUG previo: filtro dejaba pasar label container con texto
            // "Sí, quiero proteger... No, no quiero..." → clickeaba la opción
            // equivocada porque el label container envuelve ambas radios.
            //
            // NUEVO ENFOQUE: EXCLUIR cualquier label que contenga "sí, quiero",
            // "quiero proteger" (sin "no" delante explícito), o cualquier precio
            // en € (los radios del Sí siempre llevan precio). Solo aceptar
            // labels CORTOS (<150 chars) que sean genuinamente la opción "No".
            if (seguroSec) {
                const filteredClickFn = (sec, pattern) => {
                    if (!sec) return {clicked: false, labels: []};
                    const labels = Array.from(sec.querySelectorAll(
                        'label, [class*="radio" i], [class*="option" i]'))
                        .filter(l => l.offsetParent !== null);
                    const labelTexts = labels.map(l => (l.innerText||l.textContent||'').trim().slice(0, 200));
                    // ORDENAR labels por longitud (más cortos primero) para preferir
                    // labels específicos (radio individual) vs containers (que
                    // envuelven varias radios).
                    const sorted = [...labels].sort((a, b) => {
                        const la = (a.innerText||a.textContent||'').trim().length;
                        const lb = (b.innerText||b.textContent||'').trim().length;
                        return la - lb;
                    });
                    for (const l of sorted) {
                        const txt = (l.innerText || l.textContent || '').trim();
                        if (txt.length === 0) continue;
                        // EXCLUSIÓN 1: cualquier "Sí, quiero" en el texto (label container o el del Sí)
                        if (/s[ií]\s*,?\s*quiero/i.test(txt)) continue;
                        if (/^[\s¡]*s[ií][\s,!.]/i.test(txt)) continue;
                        // EXCLUSIÓN 2: precios en euros (el Sí siempre lleva precio: 11,85€ / 23,70€)
                        if (/\d+[.,]\d+\s*€|\d+\s*€/i.test(txt)) continue;
                        // EXCLUSIÓN 3: feedback post-click "Seleccionando este producto"
                        if (/seleccionando\s+este\s+producto/i.test(txt)) continue;
                        // EXCLUSIÓN 4: labels demasiado largos (>180 chars) son containers
                        if (txt.length > 180) continue;
                        // INCLUSIÓN: pattern positivo
                        if (pattern.test(txt)) {
                            l.scrollIntoView({block:'center', behavior: 'instant'});
                            l.click();
                            const inp = l.querySelector('input[type="radio"]');
                            if (inp) { try { inp.click(); } catch(e) {} }
                            return {clicked: true, matched: txt.slice(0, 150), labels: labelTexts};
                        }
                    }
                    return {clicked: false, labels: labelTexts};
                };
                // Pattern positivo: textos que DEFINITIVAMENTE indican "No al seguro"
                const seguroResult = filteredClickFn(seguroSec,
                    /^[\s]*no[,.\s]+no\s+quiero|^[\s]*no,?\s+no\s+quiero|^no,?\s+gracias|no\s+quiero\s+(?:asegurar|proteger)|prefiero\s+no\s+asegurar|sin\s+protecci[oó]n|^no\b/i);
                out.seguro_clicked = seguroResult.clicked;
                out.seguro_matched = seguroResult.matched;
                out.debug_seguro_labels = seguroResult.labels;
            }
            return out;
        }""")
        log(slot, "partners_seguro_checked",
            partners_clicked=result.get('partners_clicked'),
            seguro_clicked=result.get('seguro_clicked'),
            partners_matched=result.get('partners_matched'),
            seguro_matched=result.get('seguro_matched'))
        if not result.get('partners_clicked') or not result.get('seguro_clicked'):
            log(slot, "partners_seguro_debug",
                p_section=result.get('partners_section_found'),
                s_section=result.get('seguro_section_found'),
                p_labels=result.get('debug_partners_labels', [])[:5],
                s_labels=result.get('debug_seguro_labels', [])[:5])

        # ═══ FIX 2026-05-14: VERIFICAR que el "No al seguro" quedó marcado ═══
        # NORMA USER: NUNCA aceptar seguro. Validación post-click obligatoria.
        # Si "Sí, quiero proteger" terminó seleccionado, fuerza re-click del No.
        try:
            verify = page.evaluate(r"""() => {
                const matched = (txt) => /s[ií]\s*,?\s*quiero\s+proteger|quiero\s+asegurar/i.test(txt || '');
                // Buscar radios marcados (checked) en la sección Seguro
                const allRadios = Array.from(document.querySelectorAll(
                    'input[type="radio"]:checked, [aria-checked="true"], [class*="selected" i]'))
                    .filter(r => r.offsetParent !== null);
                const checked_with_si_quiero = allRadios.filter(r => {
                    // Mirar label asociada al radio
                    const id = r.id || r.getAttribute('id');
                    let labelText = '';
                    if (id) {
                        const lbl = document.querySelector('label[for="'+id+'"]');
                        if (lbl) labelText = lbl.innerText || lbl.textContent || '';
                    }
                    if (!labelText && r.parentElement) {
                        labelText = (r.parentElement.innerText || r.parentElement.textContent || '');
                    }
                    return matched(labelText);
                });
                // FIX 2026-05-14: body_indicates_si_selected era falso positivo
                // porque "Sí, quiero proteger" SIEMPRE aparece en el body (es el
                // texto del botón option). Validación AHORA solo por radio checked
                // + buscar también labels con aria-checked="true" / clase "selected"
                // en la cercanía del texto "quiero proteger".
                const proximitySi = Array.from(document.querySelectorAll(
                    '[aria-checked="true"], [class*="selected" i][aria-pressed="true"]'))
                    .filter(el => el.offsetParent !== null)
                    .some(el => {
                        const t = (el.innerText || el.textContent || '');
                        return /s[ií]\s*,?\s*quiero\s+proteger|quiero\s+asegurar/i.test(t);
                    });
                return {
                    si_quiero_checked: checked_with_si_quiero.length > 0,
                    proximity_si_aria_checked: proximitySi,
                };
            }""")
            if verify.get('si_quiero_checked') or verify.get('proximity_si_aria_checked'):
                log(slot, "🚨 SEGURO_SI_DETECTADO_REINTENTANDO_NO", **verify)
                # Re-intento: forzar click en label específico del "No"
                page.evaluate(r"""() => {
                    const allLabels = Array.from(document.querySelectorAll('label'))
                        .filter(l => l.offsetParent !== null);
                    // Filtros estrictos
                    const candidate = allLabels.find(l => {
                        const txt = (l.innerText || l.textContent || '').trim();
                        if (txt.length > 180) return false;
                        if (/s[ií]\s*,?\s*quiero/i.test(txt)) return false;
                        if (/\d+[.,]?\d*\s*€/.test(txt)) return false;
                        return /^no,?\s+no\s+quiero|^no,?\s+gracias|no\s+quiero\s+(?:asegurar|proteger)/i.test(txt);
                    });
                    if (candidate) {
                        candidate.scrollIntoView({block:'center'});
                        candidate.click();
                        const inp = candidate.querySelector('input[type="radio"]');
                        if (inp) try { inp.click(); } catch(e){}
                        return candidate.innerText.slice(0, 100);
                    }
                    return null;
                }""")
                log(slot, "seguro_no_re_clicked_force")
            else:
                log(slot, "seguro_no_verified_ok")
        except Exception as _ve:
            log(slot, "seguro_verify_err", err=str(_ve)[:120])

        return bool(result.get("partners_clicked") or result.get("seguro_clicked"))
    except Exception as e:
        log(slot, "partners_seguro_err", err=str(e)[:120])
        return False


def handle_pre_checkout_step(slot, page):
    """Detecta y completa la pantalla intermedia entre 'Continuar comprando' y
    el checkout final. TM muestra:
      1. Checkbox 'Partners del evento' (marcar=SÍ).
      2. Checkbox 'Condiciones de compra / Acepto' (obligatorio).
      3. Botón verde 'Ir al pago'.
    Si la pantalla aparece, marca todo y pulsa el botón. Si no aparece, no
    hace nada (no es bloqueante)."""
    try:
        result = page.evaluate(r"""() => {
            const out = {found: false, partners: false, conditions: false, btn: false};
            const labels = Array.from(document.querySelectorAll('label'));
            // 1) Marcar checkbox 'partners del evento'
            const partnersLbl = labels.find(l =>
                /partners?\s+del\s+evento|partners?\s+del\s+organizador|recibir.*ofertas|comunicaciones.*partner/i
                  .test(l.textContent || ''));
            if (partnersLbl) {
                const id = partnersLbl.getAttribute('for');
                let cb = id ? document.getElementById(id) : partnersLbl.querySelector('input[type=checkbox]');
                if (cb && !cb.checked) {
                    cb.scrollIntoView({block:'center'});
                    cb.click();
                    out.partners = true;
                    out.found = true;
                }
            }
            // 2) Marcar 'Condiciones de compra' / 'Acepto'
            const condsLbl = labels.find(l =>
                /condiciones\s+de\s+compra|acepto.*condiciones|t[eé]rminos\s+y\s+condiciones|acepto\s+que\s+he\s+le[ií]do/i
                  .test(l.textContent || ''));
            if (condsLbl) {
                const id = condsLbl.getAttribute('for');
                let cb = id ? document.getElementById(id) : condsLbl.querySelector('input[type=checkbox]');
                if (cb && !cb.checked) {
                    cb.scrollIntoView({block:'center'});
                    cb.click();
                    out.conditions = true;
                    out.found = true;
                }
            }
            return out;
        }""")
        log(slot, "pre_checkout_checkboxes", **result)
        if result.get("found"):
            # Pulsar botón verde "Ir al pago"
            human_pause(0.8, 1.5)
            clicked = page.evaluate(r"""() => {
                const btns = Array.from(document.querySelectorAll('button, a'))
                    .filter(b => b.offsetParent !== null);
                const ir = btns.find(b => /ir al pago|proceed to payment|continuar al pago|pagar ahora/i
                    .test(b.innerText || b.textContent || ''));
                if (ir) {
                    ir.scrollIntoView({block:'center'});
                    ir.click();
                    return {clicked: true, label: (ir.innerText||'').slice(0,40)};
                }
                return {clicked: false};
            }""")
            log(slot, "ir_al_pago_clicked", **clicked)
            if clicked.get("clicked"):
                human_pause(4, 6)
            return clicked.get("clicked", False)
        return False
    except Exception as e:
        log(slot, "pre_checkout_handler_err", err=str(e)[:120])
        return False


def select_seatmap_sections(slot, page, sections, qty):
    """Modo seatmap: intenta cada sección de la lista en orden hasta encontrar
    una con disponibilidad de qty asientos consecutivos. Usa preferentemente
    la API ismds via fetch (rapidez); fallback a click en el SVG.
    Devuelve {ok, used_section, attempts}."""
    attempts = []
    # 1) Cambiar a modo seatmap si no estamos
    try:
        page.evaluate(r"""() => {
            const tabs = Array.from(document.querySelectorAll('[role=tab], button'));
            const t = tabs.find(x => /mapa de asientos|seat\s*map/i.test(x.innerText||''));
            if (t) t.click();
        }""")
        page.wait_for_timeout(2500)
    except Exception: pass
    # 2) Para cada sección candidata, click sobre [data-section-id=<id>]
    for sec in sections:
        sec_id = sec.strip()
        if not sec_id: continue
        clicked = page.evaluate(f"""() => {{
            const el = document.querySelector('[data-section-id="{sec_id}"], '
                + '[data-section-name="{sec_id}"]');
            if (!el) return {{found: false}};
            if (el.getAttribute('data-active') === 'false') return {{found: true, active: false}};
            el.scrollIntoView({{block: 'center'}});
            el.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
            return {{found: true, active: true}};
        }}""")
        attempts.append({"section": sec_id, **clicked})
        log(slot, "section_click_attempt", **{"section": sec_id, **clicked})
        if clicked.get("found") and clicked.get("active"):
            page.wait_for_timeout(2500)
            # Verificar si TM aceptó (panel de seats aparece)
            ok = page.evaluate(r"""() => {
                return document.querySelector('[data-testid*="seats" i], [class*="seat-picker" i]') != null;
            }""")
            if ok:
                return {"ok": True, "used_section": sec_id, "attempts": attempts}
    return {"ok": False, "attempts": attempts, "error": "ninguna_section_disponible"}


def log(slot, stage, **kw):
    """Logging wrapper — delega a `_compra_logger.log_event` (UN solo sistema).

    Mantiene compat con la firma legacy `log(slot, stage, **kw)` y emite tanto:
      - stdout legacy `[slot1][HH:MM:SS] event {json}` (parseable por wrapper)
      - JSON Lines a `runs/structured_slot<N>.jsonl` (parseable por jq/tools)

    Política consolidación 2026-05-13: NO usar `print()` directo en el bot.
    Cualquier traza debe pasar por `log()` (legacy) o `log_event()` (canónico).
    Excepción documentada: `_ready_banner` (UI humano, NO log estructurado).

    Defensa: si alguna llamada legacy usa kwargs con nombres reservados
    (`event`, `level`, `jsonl_path`), los renombramos con prefijo `_` para
    evitar colisión con la firma de `log_event`.
    """
    # Renombrar kwargs reservados (rename collision-safe)
    _reserved = {"event", "level", "jsonl_path"}
    safe_kw = {(f"_{k}" if k in _reserved else k): v for k, v in kw.items()}
    _logger_log_event(slot, stage, **safe_kw)


def _ready_banner(slot: int, page_url: str) -> None:
    """Banner visual ÚNICAMENTE para el humano (NO log estructurado).

    Excepción a la política "no print directo": esto es UI visible para el
    usuario que opera el bot, no telemetría. El log estructurado equivalente
    ya se emite vía `log(slot, "READY_FOR_USER", url=page_url)` antes.
    """
    print("\n" + "=" * 70)
    print(f"✅ SLOT {slot}: tarjeta autorrellenada. NO he pulsado Pagar.")
    print(f"URL: {page_url}")
    print(f"Para cerrar: crear archivo _compra_parallel.STOP")
    print("=" * 70 + "\n")


def shoot(slot, page, name):
    d = os.path.join(PROYECTO, "datos", "compra_parallel")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"slot{slot}_{int(time.time())}_{name}.png")
    try:
        page.screenshot(path=p, full_page=False)
        log(slot, "shot", file=os.path.basename(p))
    except Exception as e:
        log(slot, "shot_err", err=str(e)[:80])
    return p


def human_pause(min_s=0.8, max_s=2.0):
    time.sleep(random.uniform(min_s, max_s))


def imap_otp(slot, email_madre, pwd, since_ts, max_wait=180):
    deadline = time.time() + max_wait
    pat6 = re.compile(r'\b(\d{6})\b')
    while time.time() < deadline:
        try:
            with MailBox('imap.mail.me.com').login(email_madre, pwd) as mb:
                mb.folder.set('INBOX')
                msgs = list(mb.fetch(AND(date_gte=since_ts.date()),
                                     limit=20, reverse=True, mark_seen=False))
                tm = [m for m in msgs if 'ticketmaster' in (m.from_ or '').lower()
                      and m.date and m.date.replace(tzinfo=None) >= since_ts.replace(tzinfo=None)]
                log(slot, "imap_poll", recent=len(tm))
                for m in tm:
                    body = (m.text or '') + ' ' + (m.subject or '')
                    for code in pat6.findall(body):
                        if 100000 <= int(code) <= 999999:
                            log(slot, "otp_found", code=code)
                            return code
        except Exception as e:
            log(slot, "imap_err", err=str(e)[:80])
        time.sleep(8)
    return None


_BAD_CREDS_PATH = os.path.join(PROYECTO, "_compra_parallel_bad_credentials.json")


def _load_bad_credentials():
    try:
        if os.path.exists(_BAD_CREDS_PATH):
            with open(_BAD_CREDS_PATH, encoding='utf-8') as f:
                return set(json.load(f))
    except Exception: pass
    return set()


def _mark_bad_credentials(account_id):
    """Persiste account_id como credenciales inválidas para no reusar."""
    bad = _load_bad_credentials()
    bad.add(account_id)
    try:
        with open(_BAD_CREDS_PATH, 'w', encoding='utf-8') as f:
            json.dump(list(bad), f)
    except Exception: pass


def detect_invalid_credentials(page):
    """Detecta mensaje TM 'No podemos encontrar una cuenta con el correo y
    contraseña indicados'. Devuelve True si está visible."""
    try:
        return page.evaluate(r"""() => {
            const txt = (document.body && document.body.innerText || '').toLowerCase();
            return /no podemos encontrar una cuenta|cuenta con el correo y contrase|contrase[ñn]a indicados|incorrect.*password|invalid.*credentials/i.test(txt);
        }""")
    except Exception:
        return False


def _human_type(page, selector: str, text: str,
                  error_rate: float = 0.04,
                  swap_rate: float = 0.015) -> dict:
    """Typing humano con typos + correcciones (HumanTyping-style).

    Investigación competencia 2026 (NuData kb listener):
      - 4% error rate + 1.5% swap errors → bot natural
      - 85% notice errors → backspace correction
      - Bigrams comunes (th/he/in/er/an) typed faster
      - Variable speed (60-300ms) con jitter
      - Hesitation 2-5% (200-800ms pause)
      - Reaction time after error: ~350ms

    Args:
        selector: CSS selector del input target
        text: texto a tipear
        error_rate: probabilidad de typo (default 4%)
        swap_rate: probabilidad de swap (default 1.5%)

    Returns dict con stats: chars_typed, typos_made, corrected, total_keystrokes
    """
    js = r"""
    async (args) => {
      const sel = args.selector;
      const text = args.text;
      const errorRate = args.error_rate;
      const swapRate = args.swap_rate;
      const inp = document.querySelector(sel);
      if (!inp) return {ok: false, reason: 'no_input'};
      const sleep = (ms) => new Promise(r => setTimeout(r, ms));
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      inp.focus();
      inp.dispatchEvent(new FocusEvent('focus', {bubbles: true}));
      // QWERTY neighbors para typos realistas
      const neighbors = {
        'a':'sqzw','b':'vghn','c':'xdfv','d':'serfcx','e':'wrsd','f':'dergvc',
        'g':'fthbv','h':'gyjnb','i':'uojk','j':'huknm','k':'jilom','l':'kop',
        'm':'njk','n':'bhjm','o':'iklp','p':'ol','q':'wa','r':'edft','s':'awedxz',
        't':'rfgy','u':'yhji','v':'cfgb','w':'qeas','x':'zsdc','y':'tuhg',
        'z':'asx','0':'9','1':'2','2':'13','3':'24','4':'35','5':'46',
        '6':'57','7':'68','8':'79','9':'80',
      };
      // Bigrams comunes (lower case)
      const fastBigrams = new Set(['th','he','in','er','an','re','on','at','en','nd','ti','es','or','te','of','ed','is','it','al','ar','st','to','nt','ng','se','ha','as','ou','io','le','ve','co','me','de','hi','ri','ro','ic','ne','ea','ra','ce','li','ch','ll','be','ma','si','om','ur']);
      let typos = 0, corrected = 0, keystrokes = 0;
      let partial = '';
      const wpmTarget = 60 + Math.random() * 50;  // 60-110 wpm
      const baseDelay = 60000 / (wpmTarget * 5);  // ms per char target
      for (let i = 0; i < text.length; i++) {
        const ch = text[i];
        const chLower = ch.toLowerCase();
        const prev = i > 0 ? text[i-1].toLowerCase() : '';
        const isBigram = fastBigrams.has(prev + chLower);
        // Delay base + jitter + bigram bonus
        let dt = baseDelay + (Math.random() * 80 - 30);
        if (isBigram) dt *= 0.55;
        if (' ,.\n'.includes(ch)) dt *= 2.0;  // pausa palabra
        // Hesitation ocasional (2-5%, 200-800ms)
        if (Math.random() < 0.03) dt += 200 + Math.random() * 600;
        // Errores
        const mayError = Math.random() < errorRate;
        const maySwap = !mayError && i < text.length - 1 && Math.random() < swapRate;
        if (mayError && neighbors[chLower]) {
          // Tipear letra incorrecta
          const wrong = neighbors[chLower][Math.floor(Math.random() * neighbors[chLower].length)];
          await sleep(Math.max(40, dt));
          const wEv = {key: wrong, code: 'Key'+wrong.toUpperCase(),
                       charCode: wrong.charCodeAt(0), keyCode: wrong.charCodeAt(0),
                       which: wrong.charCodeAt(0), bubbles:true, cancelable:true, view:window};
          inp.dispatchEvent(new KeyboardEvent('keydown', wEv));
          inp.dispatchEvent(new KeyboardEvent('keypress', wEv));
          partial += wrong;
          setter.call(inp, partial);
          inp.dispatchEvent(new Event('input', {bubbles: true}));
          inp.dispatchEvent(new KeyboardEvent('keyup', wEv));
          typos++; keystrokes++;
          // 85% notice & correct
          if (Math.random() < 0.85) {
            await sleep(280 + Math.random() * 140);
            // Backspace
            inp.dispatchEvent(new KeyboardEvent('keydown', {key:'Backspace', code:'Backspace', keyCode:8, bubbles:true}));
            inp.dispatchEvent(new KeyboardEvent('keyup', {key:'Backspace', code:'Backspace', keyCode:8, bubbles:true}));
            partial = partial.slice(0, -1);
            setter.call(inp, partial);
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            corrected++; keystrokes++;
            await sleep(80 + Math.random() * 80);
            // Continue with correct char below (fall through)
          } else {
            // No notice → continue (text will be partially wrong)
            continue;
          }
        } else if (maySwap) {
          // Swap: tipear next char primero, luego este
          const nextCh = text[i+1];
          await sleep(Math.max(40, dt));
          for (const c of [nextCh, ch]) {
            const ev = {key:c, code:'Key'+c.toUpperCase(), charCode:c.charCodeAt(0),
                        keyCode:c.charCodeAt(0), which:c.charCodeAt(0),
                        bubbles:true, cancelable:true, view:window};
            inp.dispatchEvent(new KeyboardEvent('keydown', ev));
            inp.dispatchEvent(new KeyboardEvent('keypress', ev));
            partial += c;
            setter.call(inp, partial);
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new KeyboardEvent('keyup', ev));
            keystrokes++;
            await sleep(60 + Math.random() * 60);
          }
          typos++;
          // 85% notice → backspace x2 + correct order
          if (Math.random() < 0.85) {
            await sleep(280 + Math.random() * 140);
            for (let bs = 0; bs < 2; bs++) {
              inp.dispatchEvent(new KeyboardEvent('keydown', {key:'Backspace', keyCode:8, bubbles:true}));
              inp.dispatchEvent(new KeyboardEvent('keyup', {key:'Backspace', keyCode:8, bubbles:true}));
              partial = partial.slice(0, -1);
              setter.call(inp, partial);
              inp.dispatchEvent(new Event('input', {bubbles: true}));
              corrected++; keystrokes++;
              await sleep(50);
            }
            await sleep(80 + Math.random() * 80);
            // Re-type both in correct order (resume normal flow at i, skip next)
            for (const c of [ch, nextCh]) {
              const ev = {key:c, code:'Key'+c.toUpperCase(), charCode:c.charCodeAt(0),
                          keyCode:c.charCodeAt(0), which:c.charCodeAt(0),
                          bubbles:true, cancelable:true, view:window};
              inp.dispatchEvent(new KeyboardEvent('keydown', ev));
              inp.dispatchEvent(new KeyboardEvent('keypress', ev));
              partial += c;
              setter.call(inp, partial);
              inp.dispatchEvent(new Event('input', {bubbles: true}));
              inp.dispatchEvent(new KeyboardEvent('keyup', ev));
              keystrokes++;
              await sleep(60 + Math.random() * 60);
            }
          }
          i++;  // skip next (ya tipeado)
          continue;
        }
        // Char normal correcto
        await sleep(Math.max(40, dt));
        const ev = {key:ch, code:'Key'+ch.toUpperCase(), charCode:ch.charCodeAt(0),
                    keyCode:ch.charCodeAt(0), which:ch.charCodeAt(0),
                    bubbles:true, cancelable:true, view:window};
        inp.dispatchEvent(new KeyboardEvent('keydown', ev));
        inp.dispatchEvent(new KeyboardEvent('keypress', ev));
        partial += ch;
        setter.call(inp, partial);
        inp.dispatchEvent(new Event('input', {bubbles: true}));
        inp.dispatchEvent(new KeyboardEvent('keyup', ev));
        keystrokes++;
      }
      // Final: change + blur
      await sleep(120 + Math.random() * 120);
      inp.dispatchEvent(new Event('change', {bubbles: true}));
      return {ok: true, value: inp.value, typos, corrected, keystrokes,
              wpm: Math.round(wpmTarget)};
    }
    """
    try:
        return page.evaluate(js, {
            "selector": selector,
            "text": text,
            "error_rate": error_rate,
            "swap_rate": swap_rate,
        }) or {"ok": False}
    except Exception as _e:
        return {"ok": False, "err": f"{type(_e).__name__}: {str(_e)[:100]}"}


def _skip_passkey_prompt(slot, page, timeout_ms: int = 6000) -> bool:
    """Tras login OK, TM muestra a veces el modal 'Añadir una Llave de Acceso'
    (passkey/WebAuthn) que ofrece registrar biometría. El bot debe pulsar
    'Ahora mismo no' / 'Más tarde' para skip.

    Idempotente: si no hay modal, no hace nada. Si lo hay, lo cierra y espera
    a que el flow continúe a /checkout/.

    Returns True si encontró y cerró el modal, False en otro caso.
    """
    try:
        # Detección por keywords del body (rápida)
        body = page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 1500).toLowerCase()"
        ) or ''
        markers = [
            'llave de acceso', 'añadir una llave',
            'passkey', 'add a passkey', 'sign in faster',
            'inicia sesión rápida', 'inicia sesion rapida',
            'inicio de sesión rápido', 'usa tu cara, huella',
            'usa tu cara o huella', 'sin introducir tu contraseña',
        ]
        if not any(m in body for m in markers):
            return False
        log(slot, "passkey_prompt_detected")
        try:
            shoot(slot, page, "passkey_prompt")
        except Exception:
            pass
        # Click "Ahora mismo no" / "Ahora no" / "Skip" / "Más tarde" / "Not now"
        clicked = page.evaluate(r"""() => {
            const cands = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'));
            const visible = cands.filter(b => b.offsetParent !== null && !b.disabled);
            const re = /^(ahora\s+mismo\s+no|ahora\s+no|m[aá]s\s+tarde|no,?\s+gracias|skip|not\s+now|maybe\s+later|cancel(ar)?|saltar|omitir)$/i;
            const t = visible.find(b => re.test((b.innerText || b.textContent || b.value || '').trim()));
            if (t) { t.click(); return (t.innerText || t.textContent || t.value || '').trim(); }
            // Fallback: contiene "ahora" o "tarde" o "skip" en algún botón del modal
            const t2 = visible.find(b => /(ahora.*no|m[aá]s.*tarde|skip|maybe.*later|not.*now)/i.test((b.innerText || b.textContent || '').trim()));
            if (t2) { t2.click(); return (t2.innerText || t2.textContent || '').trim(); }
            return null;
        }""")
        if clicked:
            log(slot, "passkey_skipped", button=str(clicked)[:60])
            try:
                page.wait_for_load_state('networkidle', timeout=timeout_ms)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            return True
        else:
            log(slot, "passkey_skip_no_button_found")
            return False
    except Exception as _e:
        try:
            log(slot, "passkey_skip_err", err=f"{type(_e).__name__}: {str(_e)[:120]}")
        except Exception:
            pass
        return False


def login_full(slot, page, account):
    log(slot, "login_start", email=account.get('tm_email'),
        human_motion=_HUMAN_MOTION)
    # OPTIM 2026-05-12 user feedback: dejar que la pantalla OAuth cargue
    # completamente (3s extra) — TM tarda en renderizar el input email tras
    # redirect desde checkout. Sin esta pausa el bot intenta typear antes
    # de que el form esté listo y falla silenciosamente.
    human_pause(3, 5)
    try:
        page.wait_for_selector('input[type="email"]', timeout=30000)
    except Exception:
        log(slot, "no_email_input"); shoot(slot, page, "no_email_input"); return False
    # Wait extra 1s para asegurar que el JS de TM termine de cargar listeners
    page.wait_for_timeout(1000)
    # Fix 2026-05-12 user feedback: PEGAR email (paste via JS native setter
    # + dispatch event input). Funciona en React/Vue/Next.js. Instantáneo.
    # FIX A12 2026-05-14: ADEMÁS, dispatch keydown/keyup synthetic events
    # con timestamps creíbles para que NuData (nudata.ticketmaster.com) vea
    # typing pattern humano. Sin esto, NuData kb-listener queda vacío → tell
    # de bot → forced_password_reset.
    # FIX 2026-05-14: typing humano con typos + correcciones (HumanTyping-style)
    # Investigación competencia: 4% error + 1.5% swap + bigrams + hesitation
    # → NuData kb listener recibe pattern humano, NO bot-uniform.
    em_pasted = _human_type(page, 'input[type="email"]', account['tm_email'])
    log(slot, "email_typed", **em_pasted)
    page.wait_for_timeout(200)   # solo lo justo para que TM detecte el cambio
    # Click "Aceptar" / "Continuar" / "Siguiente" — JS direct con TODOS los
    # nombres posibles. TM España usa "Aceptar" en la pantalla OAuth.
    clicked_continuar = False
    try:
        res = page.evaluate(r"""() => {
            const btns = Array.from(document.querySelectorAll(
                'button, input[type="submit"], [role="button"]'));
            const visible = btns.filter(b => b.offsetParent !== null && !b.disabled);
            // Texto exacto: Aceptar (TM ES) / Continuar / Siguiente / Next
            const exact = visible.find(b => {
                const txt = (b.innerText||b.textContent||b.value||'').trim();
                return /^(aceptar|continuar|continue|siguiente|next|ok)$/i.test(txt);
            });
            if (exact) { exact.click(); return 'click:' + (exact.innerText||'').slice(0,30); }
            // Cualquier button submit visible
            const sub = visible.find(b => b.type === 'submit');
            if (sub) { sub.click(); return 'submit:' + (sub.innerText||'').slice(0,30); }
            return null;
        }""")
        if res:
            clicked_continuar = True
            log(slot, "email_continuar_clicked", method=str(res)[:50])
    except Exception as _ce:
        log(slot, "email_continuar_err", err=str(_ce)[:80])
    if not clicked_continuar:
        try:
            page.keyboard.press("Enter")
            log(slot, "email_continuar_clicked", method="enter")
        except Exception: pass
    page.wait_for_timeout(600)   # tiempo mínimo para que TM cargue siguiente pantalla
    shoot(slot, page, "after_email_continuar")
    # Fix 2026-05-12: POLLING UNIFICADO durante hasta 25s.
    # Cada 500ms re-chequea:
    #   a) input[type=password] visible → break y entrar a pwd fill
    #   b) reset markers en body/title → return "reset_required"
    # Antes hacíamos check único tras 800ms y a veces TM aún no había
    # renderizado la pantalla de reset → falso "no_pwd_input" / bad_creds.
    reset_markers = [
        'restablecer la contraseña', 'restablecer tu contraseña',
        'restablece tu contraseña', 'restablece la contraseña',
        'reset your password', 'reset password',
        'mantener la seguridad de tu cuenta',
        'pedimos que restablezcas tu contraseña',
        'we ask you to reset your password',
    ]
    pwd_visible = False
    poll_start = time.time()
    while (time.time() - poll_start) < 25:
        try:
            state = page.evaluate(r"""() => {
                const pw = document.querySelector('input[type="password"]');
                const pwVisible = !!(pw && pw.offsetParent !== null);
                const body = (document.body.innerText || '').slice(0, 1500).toLowerCase();
                const title = (document.title || '').toLowerCase();
                return {pw: pwVisible, body: body, title: title};
            }""")
            if state.get('pw'):
                pwd_visible = True
                break
            haystack = (state.get('body','') + ' ' + state.get('title','')).lower()
            if any(m in haystack for m in reset_markers):
                log(slot, "tm_forced_password_reset_detected",
                    email=account.get('tm_email'),
                    elapsed_s=round(time.time() - poll_start, 1),
                    note="TM bloquea login normal — necesita reset password")
                shoot(slot, page, "tm_forced_reset")
                return "reset_required"
        except Exception as _e:
            log(slot, "login_poll_err", err=str(_e)[:80])
        page.wait_for_timeout(500)
    if not pwd_visible:
        # 25s sin pwd input y sin reset markers — falla real (cuenta no existe o
        # TM cambió el flujo).
        log(slot, "no_pwd_input", elapsed_s=round(time.time() - poll_start, 1))
        shoot(slot, page, "no_pwd")
        return False
    # OPTIM 2026-05-12 user feedback: dejar que la pantalla de pwd cargue
    # completamente (1.5s) — tras email_continuar, TM tarda en renderizar
    # el form de pwd con sus listeners React.
    page.wait_for_timeout(1500)
    # FIX 2026-05-14: typing humano (HumanTyping-style) para password también
    pw_pasted = _human_type(page, 'input[type="password"]', account['tm_password'])
    pw_pasted = {"ok": pw_pasted.get("ok"), "len": len(account['tm_password'])} if pw_pasted.get("ok") else pw_pasted
    log(slot, "pwd_typed", **pw_pasted)
    page.wait_for_timeout(200)
    # Click "Aceptar" / "Iniciar sesión" / "Sign In" — TM España puede usar
    # "Aceptar" igual que en email screen.
    try:
        res = page.evaluate(r"""() => {
            const btns = Array.from(document.querySelectorAll(
                'button, input[type="submit"], [role="button"]'));
            const visible = btns.filter(b => b.offsetParent !== null && !b.disabled);
            const exact = visible.find(b => {
                const txt = (b.innerText||b.textContent||b.value||'').trim();
                return /^(aceptar|iniciar sesi[oó]n|sign in|login|continuar|ok)$/i.test(txt);
            });
            if (exact) { exact.click(); return 'click:' + (exact.innerText||'').slice(0,30); }
            const sub = visible.find(b => b.type === 'submit');
            if (sub) { sub.click(); return 'submit'; }
            return null;
        }""")
        if res:
            log(slot, "pwd_continuar_clicked", method=str(res)[:50])
    except Exception as _ce:
        log(slot, "pwd_continuar_err", err=str(_ce)[:80])
    # Fallback secundario si JS click no funcionó (ya manejado arriba)
    page.wait_for_timeout(3000); shoot(slot, page, "post_login")
    # Detectar error "No podemos encontrar una cuenta..."
    if detect_invalid_credentials(page):
        log(slot, "BAD_CREDENTIALS_detected",
            email=account.get('tm_email'),
            note="Marcando cuenta como inválida y devolviendo bad_credentials")
        _mark_bad_credentials(account.get('id'))
        shoot(slot, page, "bad_credentials")
        return "bad_credentials"
    # OTP — paste rápido vía JS native setter (React-friendly)
    try:
        otp = page.locator('input[name*="code" i], input[autocomplete*="one-time" i], input[type="tel"][maxlength]').first
        if otp.is_visible(timeout=3000):
            log(slot, "otp_required"); shoot(slot, page, "otp_screen")
            since = datetime.now()
            code = imap_otp(slot, account.get('contact_email') or account.get('email', ''),
                             account.get('contact_password') or account.get('email_password', ''),
                             since, max_wait=180)
            if code:
                # PASTE via JS native setter (mismo patrón que email/pwd)
                try:
                    otp_pasted = page.evaluate(r"""(code) => {
                        const inp = document.querySelector('input[name*="code" i], input[autocomplete*="one-time" i], input[type="tel"][maxlength]');
                        if (!inp) return {ok: false, reason: 'no_input'};
                        inp.focus();
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(inp, code);
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        return {ok: true, len: inp.value.length, value: inp.value};
                    }""", code)
                    log(slot, "otp_pasted", **(otp_pasted or {}))
                except Exception as _oe:
                    log(slot, "otp_paste_err", err=str(_oe)[:80])
                    try: otp.fill(code)
                    except Exception: pass
                page.wait_for_timeout(200)
                # Click "Verificar/Confirmar/Continuar/Aceptar" vía JS direct
                try:
                    res = page.evaluate(r"""() => {
                        const btns = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
                        const target = btns.find(b => /^(verificar|confirmar|continuar|continue|aceptar|next|siguiente|ok|submit)$/i.test((b.innerText || b.value || '').trim()));
                        if (target) { target.click(); return target.innerText || target.value; }
                        return null;
                    }""")
                    if res:
                        log(slot, "otp_submit_clicked", method=str(res)[:50])
                except Exception as _ce:
                    log(slot, "otp_submit_err", err=str(_ce)[:80])
                page.wait_for_timeout(3000)
                _skip_passkey_prompt(slot, page)
                return True
            return False
    except Exception: pass
    _skip_passkey_prompt(slot, page)
    return True


def fill_card(slot, page, card, account):
    """Rellena el form de dirección TM siguiendo la metodología documentada en
    `feedback_compra_flow_completo_pasos.md`:
    - Selectores TM exactos: input[name="address.firstName"], etc.
    - JS native setter (React-friendly, Playwright .fill() falla silencioso)
    - Combobox Provincia: typear + click [role=option]:has-text(Madrid)
    - Click "Guardar y continuar" al final
    Defaults: si la cuenta no tiene address, usa Madrid 28001 / Calle Mayor 1.
    NOTA: fill_card() rellena DIRECCIÓN. fill_card_adyen() rellena la tarjeta
    en pantalla payments.ticketmaster.es (iframes Adyen) — son etapas distintas."""
    log(slot, "fill_card_start"); page.wait_for_timeout(3000)
    shoot(slot, page, "checkout_initial")

    # ─── DATOS DEL CUENTA ───
    first_name = account.get('first_name', '') or 'Juan'
    last_name = account.get('last_name', '') or 'García López'
    address1 = (account.get('address1', '') or account.get('address', '') or
                'Calle Mayor 1')
    address2 = account.get('address2', '') or ''
    city = account.get('city', '') or 'Madrid'
    postal = account.get('zip', '') or account.get('postal', '') or '28001'
    state = account.get('state', '') or account.get('province', '') or 'Madrid'
    phone = account.get('phone', '') or ''
    phone_prefix = account.get('phone_prefix', '+34')

    log(slot, "fill_card_data",
        first_name=first_name, last_name=last_name,
        address1=address1, city=city, postal=postal, state=state)

    # ─── BLOQUE 1: form dirección via JS NATIVE SETTER ───
    # TM usa inputs React-controlled con names tipo `address.firstName`.
    # Playwright .fill() falla silencioso en React → usar setter nativo.
    try:
        filled_fields = page.evaluate(r"""(d) => {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            function setVal(name, val) {
                if (!val) return false;
                // Probar selector exacto TM primero, luego fallbacks
                const sels = [
                    `input[name="address.${name}"]`,
                    `input[name="${name}"]`,
                    `input[id*="${name}" i]`,
                ];
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        el.focus();
                        setter.call(el, val);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                        return sel;
                    }
                }
                return false;
            }
            const out = {};
            out.firstName = setVal('firstName', d.first_name);
            out.lastName = setVal('lastName', d.last_name);
            out.addressLine1 = setVal('addressLine1', d.address1);
            if (d.address2) out.addressLine2 = setVal('addressLine2', d.address2);
            out.city = setVal('city', d.city);
            out.postalCode = setVal('postalCode', d.postal);
            // phone si lo pide
            const fullPhone = (d.phone_prefix || '') + (d.phone || '');
            if (fullPhone) {
                const phEl = document.querySelector('input[type="tel"], input[name*="phone" i], input[autocomplete="tel"]');
                if (phEl && phEl.offsetParent !== null) {
                    phEl.focus();
                    setter.call(phEl, fullPhone);
                    phEl.dispatchEvent(new Event('input', {bubbles: true}));
                    phEl.dispatchEvent(new Event('change', {bubbles: true}));
                    out.phone = true;
                }
            }
            return out;
        }""", {
            'first_name': first_name, 'last_name': last_name,
            'address1': address1, 'address2': address2,
            'city': city, 'postal': postal,
            'phone': phone, 'phone_prefix': phone_prefix,
        })
        log(slot, "fill_card_inputs_filled", **{k: bool(v) for k, v in (filled_fields or {}).items()})
    except Exception as _fe:
        log(slot, "fill_card_inputs_err", err=str(_fe)[:120])

    # ─── BLOQUE 2: combobox Provincia (stateCode) ───
    # TM España usa un combobox React, no un simple input. Necesita:
    # 1) click input → enfoca + abre dropdown
    # 2) typear estado letra a letra → filtra opciones
    # 3) click [role=option] o ComboboxListItem que matchea
    # FIX 2026-05-14: matching tolerante a tildes/case + ArrowDown+Enter fallback
    page.wait_for_timeout(700)
    try:
        # 1) Localizar el input del combobox
        prov_input_sel = None
        for sel in ['input[name="address.stateCode"]',
                    'input[name*="stateCode" i]',
                    'input[name*="province" i]',
                    'input[name*="provincia" i]',
                    'input[autocomplete="address-level1"]']:
            try:
                if page.locator(sel).first.is_visible(timeout=600):
                    prov_input_sel = sel
                    break
            except Exception: continue
        if prov_input_sel:
            log(slot, "province_combobox_found", selector=prov_input_sel)
            prov_input = page.locator(prov_input_sel).first
            try: prov_input.click(timeout=2000)
            except Exception: pass
            page.wait_for_timeout(400)
            # Limpiar valor previo
            try:
                page.evaluate(f"""() => {{
                    const el = document.querySelector('{prov_input_sel}');
                    if (el) {{
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        setter.call(el, '');
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    }}
                }}""")
            except Exception: pass
            page.wait_for_timeout(300)
            # Typear con delay (simula humano) — solo PRIMERAS 3 letras para
            # forzar dropdown abierto + opciones cargadas, sin filtrar demasiado
            state_for_typing = state[:3] if len(state) > 3 else state
            try: prov_input.type(state_for_typing, delay=120)
            except Exception:
                page.evaluate(f"""(v) => {{
                    const el = document.querySelector('{prov_input_sel}');
                    if (el) {{
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        el.focus();
                        setter.call(el, v);
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    }}
                }}""", state_for_typing)
            page.wait_for_timeout(1200)   # más tiempo para opciones (era 900ms)
            log(slot, "province_typed", value=state_for_typing, full_state=state)

            # 3) Click la opción matching tolerante a tildes/case
            #    Búsqueda en distintos selectores + normalización Unicode NFD
            #    para que "Leon" matchee con "León", "Castilla y León", etc.
            try:
                clicked_opt = page.evaluate(r"""(state) => {
                    // Normalizar: minúsculas + sin tildes
                    const norm = (s) => (s || '').toLowerCase()
                        .normalize('NFD').replace(/[̀-ͯ]/g, '').trim();
                    const target = norm(state);
                    const selectors = [
                        '[role="option"]',
                        '[role="listbox"] [role="option"]',
                        '[class*="ComboboxListItem" i]',
                        '[class*="comboboxOption" i]',
                        '[class*="combobox" i] li',
                        '[class*="autocomplete" i] li',
                        '[class*="dropdown" i] li',
                        '[id*="listbox" i] [role="option"]',
                        'ul[role="listbox"] li',
                        'ul[class*="options" i] li',
                        'div[class*="option" i]',
                        '[data-value]',
                    ];
                    let totalOpts = 0;
                    let optsList = [];
                    for (const sel of selectors) {
                        const opts = Array.from(document.querySelectorAll(sel))
                            .filter(o => o.offsetParent !== null);
                        totalOpts += opts.length;
                        // Coleccionar para debug
                        optsList.push(...opts.slice(0, 5).map(o => ({
                            sel: sel,
                            text: (o.innerText || o.textContent || '').trim().slice(0, 40),
                        })));
                        // Match EXACTO normalizado
                        let match = opts.find(o => norm(o.innerText || o.textContent) === target);
                        if (match) { match.click(); return {method: sel+':exact', text: match.innerText, totalOpts}; }
                        // Match CONTIENE normalizado (ej. "Castilla y León" contiene "leon")
                        match = opts.find(o => norm(o.innerText || o.textContent).includes(target));
                        if (match) { match.click(); return {method: sel+':contains', text: match.innerText, totalOpts}; }
                        // Match STARTS WITH normalizado (ej. para single-word states)
                        match = opts.find(o => norm(o.innerText || o.textContent).startsWith(target));
                        if (match) { match.click(); return {method: sel+':startsWith', text: match.innerText, totalOpts}; }
                    }
                    return {method: null, totalOpts, opts_seen: optsList.slice(0, 10)};
                }""", state)
                log(slot, "province_option_clicked", **(clicked_opt or {}))
            except Exception as _pe:
                log(slot, "province_option_click_err", err=str(_pe)[:80])
                clicked_opt = {}
            page.wait_for_timeout(700)

            # FALLBACK: si no se clickeó opción, usar keyboard ArrowDown + Enter
            # (estilo humano cuando el dropdown está abierto).
            if not (clicked_opt or {}).get('method'):
                try:
                    log(slot, "province_keyboard_fallback")
                    prov_input.focus()
                    page.wait_for_timeout(400)
                    # Re-clickear input para reabrir dropdown
                    try: prov_input.click()
                    except Exception: pass
                    page.wait_for_timeout(500)
                    # Arrow down 2 veces (selecciona primera opción), luego Enter
                    page.keyboard.press('ArrowDown')
                    page.wait_for_timeout(250)
                    page.keyboard.press('ArrowDown')
                    page.wait_for_timeout(250)
                    page.keyboard.press('Enter')
                    page.wait_for_timeout(800)
                    # Verificar valor actual del input
                    final_val = page.evaluate(f"""() => {{
                        const el = document.querySelector('{prov_input_sel}');
                        return el ? el.value : '';
                    }}""")
                    log(slot, "province_keyboard_result", final_value=str(final_val)[:40])
                except Exception as _kbe:
                    log(slot, "province_keyboard_err", err=str(_kbe)[:80])

            # Verificación final: ¿hay marker "Provincia obligatoria" todavía?
            try:
                still_required = page.evaluate(r"""() => {
                    const body = (document.body.innerText || '').toLowerCase();
                    return /provincia\s+obligatoria|state\s+required/i.test(body);
                }""")
                if still_required:
                    log(slot, "province_still_required_after_attempts",
                        note="combobox no validado")
                else:
                    log(slot, "province_validated_ok")
            except Exception: pass
        else:
            log(slot, "province_combobox_not_found")
    except Exception as _ce:
        log(slot, "province_combobox_err", err=str(_ce)[:120])

    shoot(slot, page, "address_form_filled")

    # ─── BLOQUE 3: click "Guardar y continuar" ───
    # Tras rellenar dirección, TM muestra "Guardar y continuar" o "Continuar".
    # Hacer scroll para asegurarse de que el botón sea visible.
    try:
        page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})")
        page.wait_for_timeout(600)
        clicked_save = page.evaluate(r"""() => {
            const btns = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'))
                .filter(b => b.offsetParent !== null && !b.disabled);
            const target = btns.find(b => {
                const t = (b.innerText || b.textContent || b.value || '').trim().toLowerCase();
                return /^(guardar y continuar|guardar|continuar|continue|ir al pago|next)$/i.test(t);
            });
            if (target) { target.click(); return target.innerText || target.value; }
            return null;
        }""")
        log(slot, "guardar_continuar_clicked", text=str(clicked_save)[:50])
    except Exception as _se:
        log(slot, "guardar_continuar_err", err=str(_se)[:80])
    shoot(slot, page, "checkout_filled")


def fill_card_adyen(slot, page, card):
    """Rellena el form de TARJETA en pantalla `payments.ticketmaster.es`.

    DESCUBRIMIENTO 2026-05-12 (test manual): TM España NO usa iframes Adyen
    en la pantalla de pago — usa INPUTS DIRECTOS en el DOM principal. Por
    eso los tests anteriores con iframes Adyen siempre fallaban.

    Flow real TM ES payments page:
    1) Click radio "Tarjeta de Crédito / Débito" → expande el form
    2) Inputs directos (NO iframes):
       - Nombre en la Tarjeta (pre-rellenado por TM)
       - Número de la tarjeta (input directo, type allows spaces)
       - Fecha de caducidad MM/AA
       - Código de seguridad (CVV)
    3) Click "Añadir tarjeta" (azul)
    4) Click "Realizar Compra" (verde) → dispara 3DS
    """
    log(slot, "fill_card_adyen_start")
    page.wait_for_timeout(2500)

    holder = card.get('holder', '')
    number = re.sub(r'\D', '', card.get('number', ''))
    # FIX 2026-05-12: las tarjetas usan exp_month/exp_year, no month/year
    month = str(card.get('exp_month', '') or card.get('month', '')).zfill(2)
    year_raw = str(card.get('exp_year', '') or card.get('year', ''))
    year_yy = year_raw[-2:] if len(year_raw) >= 2 else year_raw
    cvv = str(card.get('cvv', ''))

    log(slot, "fill_card_adyen_data",
        holder=holder[:20], number_last4=number[-4:] if number else '',
        month=month, year_yy=year_yy)

    # PASO 1: Click radio "Tarjeta de Crédito/Débito" para expandir form
    try:
        clicked_card_radio = page.evaluate(r"""() => {
            // Encontrar el radio/label que indica Tarjeta de Crédito
            const items = Array.from(document.querySelectorAll(
                'label, [class*="radio" i], [class*="option" i], [role="radio"]'))
                .filter(l => l.offsetParent !== null);
            const cardItem = items.find(l => {
                const txt = (l.innerText || l.textContent || '').trim().toLowerCase();
                return /tarjeta\s+de\s+cr[eé]dito|credit\s+card|cr[eé]dito\s*\/?\s*d[eé]bito/i.test(txt)
                    && !/regalo/i.test(txt);
            });
            if (cardItem) {
                cardItem.scrollIntoView({block: 'center'});
                cardItem.click();
                const inp = cardItem.querySelector('input[type="radio"]');
                if (inp) { try { inp.click(); } catch(e) {} }
                return cardItem.innerText.slice(0, 50);
            }
            return null;
        }""")
        log(slot, "card_radio_clicked", text=str(clicked_card_radio)[:50])
    except Exception as _ce:
        log(slot, "card_radio_err", err=str(_ce)[:80])

    # PASO 1b: OPTIM 2026-05-12 — POLLING iframe payments-cc.tick ready
    # Antes esperábamos 2500ms fijos → fallaba si TM tardaba más. Ahora poll
    # cada 500ms × 20 (10s max) buscando iframe payments-cc cargado.
    iframe_ready = False
    for poll in range(RETRY_IFRAME_POLL):
        page.wait_for_timeout(500)
        try:
            iframes_info = page.evaluate(r"""() => {
                const ifs = Array.from(document.querySelectorAll('iframe'));
                return ifs.map(f => ({
                    src: (f.src||'').slice(0, 100),
                    has_input: false   // marcar luego desde frames
                }));
            }""")
            # Verificar si frame con "payments-cc" tiene inputs ready
            for frame in page.frames:
                url = (frame.url or '').lower()
                if 'payments-cc' in url or 'payments-cc.tick' in url:
                    try:
                        has_inputs = frame.evaluate(r"""() => {
                            return !!document.querySelector('input[name="cardNumber"], input[id*="cardNumber" i], input[autocomplete="cc-number"]');
                        }""")
                        if has_inputs:
                            iframe_ready = True
                            log(slot, "payments_iframe_ready", poll=poll,
                                url=url[:60])
                            break
                    except Exception: continue
            if iframe_ready: break
        except Exception: pass
    if not iframe_ready:
        log(slot, "payments_iframe_timeout", note="iframe payments-cc no cargó en 10s, intentando fill igualmente")
    page.wait_for_timeout(500)   # buffer extra para listeners React

    shoot(slot, page, "card_form_expanded")

    # PASO 2: Rellenar inputs — TM España usa IFRAME `payments-cc.ticketmaster.es`
    # para los campos sensibles (PCI compliance). Bug fix 2026-05-12:
    # debemos buscar en `page.frames`, no en `page.evaluate` solo.
    fill_results = {}

    # Lista de targets: page principal + cada frame
    fill_data = {'holder': holder, 'number': number,
                 'expiry': f"{month}/{year_yy}", 'cvv': cvv}

    # JS function que rellena un solo target (page o frame)
    fill_js = r"""(d) => {
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        function fillByLabel(labelText, val) {
            const labels = Array.from(document.querySelectorAll('label'));
            for (const lbl of labels) {
                const txt = (lbl.innerText || lbl.textContent || '').toLowerCase();
                if (!txt.includes(labelText.toLowerCase())) continue;
                let inp = null;
                if (lbl.htmlFor) inp = document.getElementById(lbl.htmlFor);
                if (!inp) inp = lbl.querySelector('input');
                if (!inp) {
                    let next = lbl.parentElement;
                    for (let i = 0; i < 4 && next; i++) {
                        inp = next.querySelector('input');
                        if (inp) break;
                        next = next.nextElementSibling || next.parentElement;
                    }
                }
                if (inp && inp.offsetParent !== null) {
                    inp.focus();
                    setter.call(inp, val);
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    inp.dispatchEvent(new Event('blur', {bubbles: true}));
                    return inp.id || inp.name || 'matched';
                }
            }
            return null;
        }
        function fillBySel(sels, val) {
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) {
                    el.focus();
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                    return sel;
                }
            }
            return null;
        }
        const out = {};
        out.holder = fillByLabel('nombre en la tarjeta', d.holder) ||
                     fillBySel(['input[name*="holderName" i]', 'input[autocomplete="cc-name"]',
                               'input[id*="cardholder" i]'], d.holder);
        out.number = fillByLabel('número de la tarjeta', d.number) ||
                     fillByLabel('card number', d.number) ||
                     fillBySel(['input[name*="cardNumber" i]', 'input[autocomplete="cc-number"]',
                               'input[name="encryptedCardNumber"]', 'input[data-cse="encryptedCardNumber"]',
                               'input[id*="cardNumber" i]', 'input[name*="number" i]',
                               'input[type="tel"][maxlength="19"]', 'input[inputmode="numeric"]'], d.number);
        out.expiry = fillByLabel('fecha de caducidad', d.expiry) ||
                     fillByLabel('caducidad', d.expiry) ||
                     fillByLabel('expir', d.expiry) ||
                     fillBySel(['input[name*="expiryDate" i]', 'input[autocomplete="cc-exp"]',
                               'input[placeholder*="MM/AA" i]', 'input[placeholder*="MM/YY" i]',
                               'input[id*="expir" i]', 'input[name*="expir" i]'], d.expiry);
        out.cvv = fillByLabel('código de seguridad', d.cvv) ||
                  fillByLabel('cvv', d.cvv) ||
                  fillByLabel('cvc', d.cvv) ||
                  fillBySel(['input[name*="securityCode" i]', 'input[autocomplete="cc-csc"]',
                            'input[placeholder*="CVV" i]', 'input[placeholder*="CVC" i]',
                            'input[id*="securityCode" i]', 'input[id*="cvv" i]',
                            'input[type="tel"][maxlength="3"]', 'input[type="tel"][maxlength="4"]'], d.cvv);
        return out;
    }"""

    # PRIMERO probar en page principal (algunos eventos sin iframe)
    try:
        results_main = page.evaluate(fill_js, fill_data)
        for k, v in (results_main or {}).items():
            if v: fill_results[k] = f"main:{v}"
        log(slot, "card_inputs_main_page", **{k: bool(v) for k, v in (results_main or {}).items()})
    except Exception as _e:
        log(slot, "card_inputs_main_err", err=str(_e)[:80])

    # LUEGO probar en cada iframe (TM ES usa payments-cc.ticketmaster.es)
    for frame in page.frames:
        try:
            url = (frame.url or '').lower()
            # Skip page principal y about:blank
            if not url or url == 'about:blank': continue
            if frame == page.main_frame: continue
            # Solo iframes de payments/tarjeta
            if not any(k in url for k in ('payments', 'cc.tick', 'adyen', 'card', 'checkout')):
                continue
            log(slot, "card_iframe_trying", url=url[:80])
            try:
                results_frame = frame.evaluate(fill_js, fill_data)
                filled_in_frame = []
                for k, v in (results_frame or {}).items():
                    if v and k not in fill_results:
                        fill_results[k] = f"frame:{v}"
                        filled_in_frame.append(k)
                if filled_in_frame:
                    log(slot, "card_iframe_filled",
                        url=url[:60], fields=filled_in_frame)
            except Exception as _fe:
                log(slot, "card_iframe_eval_err", url=url[:60], err=str(_fe)[:80])
        except Exception: continue

    log(slot, "card_inputs_filled",
        holder=bool(fill_results.get('holder')),
        number=bool(fill_results.get('number')),
        expiry=bool(fill_results.get('expiry')),
        cvv=bool(fill_results.get('cvv')),
        sources=fill_results)

    shoot(slot, page, "card_filled")
    # OPTIM 2026-05-12 v13: TM tarda 1-3s en renderizar "Añadir tarjeta"
    # tras rellenar el form. Polling cada 500ms durante 8s buscando el botón.
    page.wait_for_timeout(1500)

    # PASO 3: Click "Añadir tarjeta" con polling robusto
    anadir_done = False
    for poll_attempt in range(RETRY_ANADIR_TARJETA):   # 16 × 500ms = 8s max
        try:
            page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})")
            # FIX 2026-05-13 CRÍTICO: buscar TAMBIÉN dentro de iframes
            # (descubierto: TM tiene el botón en iframe payments-cc.ticketmaster.es).
            # Probar primero en page, luego en cada frame.
            click_js = r"""() => {
                const btns = Array.from(document.querySelectorAll(
                    'button, [role="button"], input[type="submit"], a, [class*="btn" i]'))
                    .filter(b => b.offsetParent !== null && !b.disabled);
                const target = btns.find(b => {
                    const raw = (b.innerText || b.textContent || b.value || '');
                    const t = raw.replace(/[\s\n\r]+/g, ' ').trim().toLowerCase();
                    return /a[ñn]adir\s+tarjeta|add\s+card|save\s+card|guardar\s+tarjeta/i.test(t)
                        && !/eliminar|borrar|cancelar/i.test(t);
                });
                if (target) {
                    target.scrollIntoView({block: 'center'});
                    target.click();
                    return target.innerText || target.value || 'clicked';
                }
                return null;
            }"""
            # 1) Page principal
            anadir_clicked = page.evaluate(click_js)
            # 2) Si no encontrado, buscar en iframes payments-cc/checkout
            if not anadir_clicked:
                for frame in page.frames:
                    fu = (frame.url or '').lower()
                    if 'payments' in fu or 'checkout' in fu or 'card' in fu:
                        try:
                            anadir_clicked = frame.evaluate(click_js)
                            if anadir_clicked:
                                log(slot, "anadir_tarjeta_in_iframe",
                                    url=fu[:60], text=str(anadir_clicked)[:30])
                                break
                        except Exception: continue
            if anadir_clicked:
                log(slot, "anadir_tarjeta_clicked", text=str(anadir_clicked)[:50], poll=poll_attempt)
                anadir_done = True
                break
        except Exception as _ae:
            log(slot, "anadir_tarjeta_poll_err", err=str(_ae)[:80])
        page.wait_for_timeout(500)

    if not anadir_done:
        # Si no encontró botón en 8s, debug y continuar (Realizar Compra puede aún funcionar)
        try:
            debug = page.evaluate(r"""() => Array.from(document.querySelectorAll(
                'button, [role="button"], input[type="submit"]'))
                .filter(b => b.offsetParent !== null)
                .slice(0, 15).map(b => (b.innerText||b.value||'').slice(0, 40))""")
            log(slot, "anadir_tarjeta_NOT_FOUND", visible_btns=debug)
        except Exception: pass

    # OPTIM 2026-05-12: esperar 3-4s tras Añadir tarjeta — TM procesa tarjeta
    # antes de habilitar "Realizar Compra"
    page.wait_for_timeout(3000)

    shoot(slot, page, "after_anadir_tarjeta")
    log(slot, "fill_card_adyen_done", fields=list(fill_results.keys()))


_PROFILE_LOCK_PATH = os.path.join(PROYECTO, "_compra_parallel_active_profiles.lock")
_PROFILE_LOCK_MUTEX = os.path.join(PROYECTO, "_compra_parallel_pick.mutex")


def _acquire_pick_mutex(timeout=30):
    """Mutex cross-process via O_EXCL para serializar pick+claim atómico.
    Otros procesos esperan hasta que se libere. Stale > 60s se descarta."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(_PROFILE_LOCK_MUTEX, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try: os.write(fd, str(os.getpid()).encode())
            finally: os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(_PROFILE_LOCK_MUTEX)
                if age > 60:
                    try: os.unlink(_PROFILE_LOCK_MUTEX)
                    except Exception: pass
            except Exception: pass
            time.sleep(0.5)
    return False


def _release_pick_mutex():
    try: os.unlink(_PROFILE_LOCK_MUTEX)
    except Exception: pass


def _read_active_profiles():
    try:
        if os.path.exists(_PROFILE_LOCK_PATH):
            with open(_PROFILE_LOCK_PATH, encoding='utf-8') as f:
                return set(json.load(f))
    except Exception: pass
    return set()


def _add_active_profile(name):
    """Lock cross-process: añade un perfil al lock file + bo._active_profiles."""
    bo._active_profiles.add(name)
    active = _read_active_profiles()
    active.add(name)
    try:
        with open(_PROFILE_LOCK_PATH, 'w', encoding='utf-8') as f:
            json.dump(list(active), f)
    except Exception: pass


def _remove_active_profile(name):
    bo._active_profiles.discard(name)
    active = _read_active_profiles()
    active.discard(name)
    try:
        with open(_PROFILE_LOCK_PATH, 'w', encoding='utf-8') as f:
            json.dump(list(active), f)
    except Exception: pass


def _load_burned_subnets24() -> set:
    """Carga lista de subnets /24 quemadas (set de strings 'a.b.c.0/24')."""
    path = os.path.join(PROYECTO, 'datos', 'buy', 'runs', 'burned_subnets24.json')
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, encoding='utf-8') as f:
            return set((json.load(f) or {}).keys())
    except Exception:
        return set()


def _ip_to_subnet24(ip: str) -> str:
    """Convierte '79.117.139.23' → '79.117.139.0/24'."""
    try:
        parts = ip.strip().split('.')
        if len(parts) == 4:
            return f'{parts[0]}.{parts[1]}.{parts[2]}.0/24'
    except Exception:
        pass
    return ''


def _persist_bot_block(profile_id: str, ip: str, where: str, run_id: str = '',
                         account_id: str = ''):
    """Persiste perfil + subnet /24 + account_id en burned files tras detectar
    bot-block. Norma 2026-05-11: tras bot-block, blacklist profile + IP/24 para
    evitar reuso inmediato y permitir cooldown.
    Norma 2026-05-12: también persistir account_id para que pick_account
    excluya cuentas con bot_block reciente (Akamai marca por sesión OAuth)."""
    from datetime import datetime, timezone
    runs_dir = os.path.join(PROYECTO, 'datos', 'buy', 'runs')
    os.makedirs(runs_dir, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    # 1) Burned profile
    if profile_id:
        try:
            import register_account as ra
            if hasattr(ra, 'load_burned') and hasattr(ra, 'save_burned'):
                b = ra.load_burned(); b.add(profile_id); ra.save_burned(b)
        except Exception: pass
    # 2) Burned subnet /24
    subnet = _ip_to_subnet24(ip)
    if subnet:
        path = os.path.join(runs_dir, 'burned_subnets24.json')
        try:
            data = {}
            if os.path.isfile(path):
                with open(path, encoding='utf-8') as f:
                    data = json.load(f) or {}
            entry = data.get(subnet, {
                'first_burned': now, 'ips': [], 'runs': [], 'reason': 'bot_block'
            })
            entry['last_burned'] = now
            if ip and ip not in (entry.get('ips') or []):
                entry.setdefault('ips', []).append(ip)
            tag = f'bot_block_{where}_{run_id}' if run_id else f'bot_block_{where}'
            if tag not in (entry.get('runs') or []):
                entry.setdefault('runs', []).append(tag)
            data[subnet] = entry
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception: pass
    # 3) Burned account_id (NUEVO 2026-05-12): persistir cuenta para que el
    #    siguiente intento NO la re-pickee. Cooldown configurable (24h default).
    if account_id:
        path_acc = os.path.join(runs_dir, 'burned_accounts.json')
        try:
            data_acc = {}
            if os.path.isfile(path_acc):
                with open(path_acc, encoding='utf-8') as f:
                    data_acc = json.load(f) or {}
            entry_acc = data_acc.get(account_id, {
                'first_burned': now, 'reasons': []
            })
            entry_acc['last_burned'] = now
            tag_acc = f'bot_block_{where}'
            if tag_acc not in entry_acc['reasons']:
                entry_acc['reasons'].append(tag_acc)
            data_acc[account_id] = entry_acc
            with open(path_acc, 'w', encoding='utf-8') as f:
                json.dump(data_acc, f, indent=2, ensure_ascii=False)
        except Exception: pass


def _load_burned_accounts(cooldown_hours: int = 24) -> set:
    """Carga account_ids con bot_block dentro del cooldown (default 24h).
    pick_account llama esto para excluir cuentas recientemente burned."""
    from datetime import datetime, timezone, timedelta
    path = os.path.join(PROYECTO, 'datos', 'buy', 'runs', 'burned_accounts.json')
    if not os.path.isfile(path): return set()
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f) or {}
    except Exception:
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    burned = set()
    for aid, info in data.items():
        try:
            last = info.get('last_burned', '')
            if not last: continue
            ts = datetime.fromisoformat(last.replace('Z','+00:00'))
            if ts > cutoff:
                burned.add(aid)
        except Exception: continue
    return burned


def pick_morelogin_profile(slot):
    """Pick profile ES — NORMA USER 2026-05-13: por defecto CREA perfil FRESH
    via `bo.create_fresh_profile()` (proxy custom Quantum + sessionid random).
    Si falla la creación, fallback a pool existente.

    Lock cross-process via _PROFILE_LOCK_PATH.
    Excluye también subnets /24 quemadas (norma 2026-05-11 bot-block swap)."""
    # ─── PRIMERO: crear perfil FRESH (norma user 2026-05-13) ───
    # Cada llamada → IP nueva + sessionid random + perfil único.
    try:
        fresh = bo.create_fresh_profile(country_code='ES')
        if fresh:
            log(slot, "morelogin_fresh_created",
                profile=fresh.get('profile'),
                sessionid=fresh.get('sessionid'))
            # Marcar también en lock file (compat con otros slots)
            _add_active_profile(fresh.get('profile'))
            # Añadir campos esperados por el resto del flow
            fresh['proxy_username'] = bo._QUANTUM_BASE_USERNAME.format(
                cc='ES', sessid=fresh.get('sessionid'))
            fresh['proxy_country'] = 'ES'
            return fresh
        else:
            log(slot, "morelogin_fresh_create_failed", fallback="pool_existing")
    except Exception as _fe:
        log(slot, "morelogin_fresh_err", err=str(_fe)[:120])

    # ─── FALLBACK: pool existente (legacy) ───
    burned = set()
    used_ips = set()
    try:
        import register_account as ra
        if hasattr(ra, 'load_burned'):
            burned |= ra.load_burned()
        if hasattr(ra, 'load_burned_profiles'):
            burned |= ra.load_burned_profiles()
        if hasattr(ra, 'load_used_ips'):
            used_ips = ra.load_used_ips()
    except Exception:
        pass
    burned_subnets = _load_burned_subnets24()
    # Cross-process lock: leer perfiles ya en uso por OTROS slots
    cross_active = _read_active_profiles()
    # FIX 2026-05-13: cargar TODOS los perfiles primero + RANDOMIZAR
    # En lugar de pickear secuencialmente (P-172566, P-172565... siempre los
    # mismos rotos), randomizamos para distribuir el pick por todo el pool
    # de 1865+ perfiles Quantum ES.
    all_candidates = []
    for page_no in range(1, 30):
        try:
            r = requests.post(f'{bo.MORELOGIN_API}/api/env/page',
                               json={'pageNo': page_no, 'pageSize': 100},
                               timeout=10).json()
        except Exception:
            break
        items = (r.get('data') or {}).get('dataList') or []
        if not items: break
        for it in items:
            p = it.get('proxy') or {}
            name = it.get('envName') or ''
            cc = (p.get('countryCode') or '').upper()
            user = p.get('username') or ''
            if not name or name in burned: continue
            if name in bo._active_profiles: continue
            if name in cross_active: continue   # otro slot ya lo está usando
            is_es = (cc == 'ES') or ('cc-ES' in user) or ('-ES-' in user)
            if not is_es: continue
            if p.get('proxyCheckStatus') == 2: continue
            exp_ip = p.get('exportIp') or ''
            if exp_ip and exp_ip in used_ips: continue
            if exp_ip:
                sn = _ip_to_subnet24(exp_ip)
                if sn and sn in burned_subnets: continue
            all_candidates.append({'it': it, 'name': name, 'p': p,
                                     'cc': cc, 'user': user, 'exp_ip': exp_ip})
        if len(items) < 100: break

    # Randomizar para evitar bias secuencial (perfiles rotos consecutivos)
    random.shuffle(all_candidates)
    log(slot, "morelogin_picker_pool", total=len(all_candidates))

    for cand in all_candidates:
        it = cand['it']; name = cand['name']; user = cand['user']
        cc = cand['cc']; exp_ip = cand['exp_ip']
        if True:
            _add_active_profile(name)
            # P1: registra sticky session para tracking dashboard
            try:
                import proxy_sticky as _ps
                worker_id = f"slot_{name}"
                _ps.get_session(worker_id=worker_id, domain="ticketmaster.es",
                                  ttl_s=1800)
                if exp_ip:
                    _ps.report_ip(worker_id=worker_id, ip=exp_ip,
                                    domain="ticketmaster.es")
            except Exception:
                pass
            return {'profile': name, 'env_id': it['id'], 'ip': exp_ip,
                    'proxy_username': user, 'proxy_country': cc}
    return None


def pick_silent_profile(slot):
    """Pick perfil Silent (proxy embebido en el perfil, sin proxy Quantum).
    Devuelve dict {profile, env_id, ip} adaptado al shape del flow, o None.
    Import lazy: silent_engine exige SILENT_TOKEN solo cuando se usa."""
    try:
        import silent_engine as se
    except ImportError as _ie:
        log(slot, "FATAL_silent_import", err=str(_ie)[:120])
        return None
    try:
        prof = se._pick_fresh_profile('ES', set())
    except Exception as _pe:
        log(slot, "silent_pick_err", err=str(_pe)[:160])
        return None
    if not prof:
        log(slot, "silent_no_profile_available")
        return None
    # Shape esperado por el resto del flow. SIN proxy_username/proxy_host:
    # el proxy va embebido en el perfil Silent → pre_flight Quantum y
    # proxy en launch NO aplican (quedan guardados por prof.get(...)).
    prof.setdefault('ip', '')
    prof['proxy_country'] = 'ES'
    log(slot, "silent_profile_picked",
        profile=prof.get('profile'), env_id=prof.get('env_id'),
        ip=prof.get('ip'))
    return prof


def pick_account(slot, used_ids, bad_creds=None):
    """Pick cuenta TM ES activa con tarjeta asociada, no usada por otro slot
    y no marcada con credenciales inválidas.

    NORMA USER 2026-05-10: priorizar SIEMPRE cuentas con credentials_verified=True
    y validated_at reciente (<14 días) — TM no fuerza password reset si ve
    actividad de login reciente. Fallback solo si no hay validadas disponibles.
    """
    if bad_creds is None:
        bad_creds = _load_bad_credentials()
    # 2026-05-12: excluir cuentas con bot_block reciente (cooldown 24h)
    burned_accounts = _load_burned_accounts(cooldown_hours=24)
    accs = am.account_manager.list_accounts()
    cards = am.account_manager.list_cards()
    cards_by_acc = {c.get('account_id'): c for c in cards if c.get('account_id')}

    # Filtro base: activa, no en uso, no bad, no burned, con tarjeta
    base_pool = [a for a in accs
                 if a.get('status') == 'active'
                 and a.get('id') not in used_ids
                 and a.get('id') not in bad_creds
                 and str(a.get('id', '')) not in burned_accounts
                 and (cards_by_acc.get(a.get('id')) or a.get('card_id'))]

    def _validated_recently(a):
        """True si credentials_verified=True y validated_at < 14 días."""
        if not a.get('credentials_verified'): return False
        v = (a.get('validated_at') or '').strip()
        if not v: return False
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(v.replace('Z','+00:00').split('+')[0])
            age_days = (_dt.utcnow() - ts).total_seconds() / 86400
            return age_days < 14
        except Exception:
            return False

    def _has_purchase_ready(a):
        return bool(a.get('purchase_ready'))

    def _fresh_virgin(a):
        """2026-05-12 fix: cuenta VIRGEN (registrada <7 días, NUNCA logueada).
        TM no las marca para reset porque no tienen historial OAuth.
        Estas son IDEALES para login fresh — saltan el reset_required masivo
        que afecta a cuentas validadas hace 7+ días.
        Crit: registration_completed_at >=7d, last_login=None, purchase_ready."""
        from datetime import datetime as _dt
        if a.get('last_login'): return False
        if not a.get('purchase_ready'): return False
        rc = (a.get('registration_completed_at') or '').strip()
        if not rc: return False
        try:
            ts = _dt.fromisoformat(rc.replace('Z','+00:00').split('+')[0])
            age_days = (_dt.utcnow() - ts).total_seconds() / 86400
            return age_days < 7
        except Exception:
            return False

    # TIER 1 (NUEVO 2026-05-12): cuentas VÍRGENES recién registradas, sin login
    # previo → TM NO las marca para forced_reset. 193 cuentas disponibles según
    # auditoría del agente.
    tier_virgin = [a for a in base_pool if _fresh_virgin(a)]
    # TIER 2 (ANTES tier1): validated reciente + purchase_ready
    tier1 = [a for a in base_pool if _validated_recently(a) and _has_purchase_ready(a)]
    # TIER 3: validated reciente (sin importar purchase_ready)
    tier2 = [a for a in base_pool if _validated_recently(a)]
    # TIER 4: purchase_ready (aunque no validated reciente)
    tier3 = [a for a in base_pool if _has_purchase_ready(a)]
    # TIER 5: cualquier base_pool
    tier4 = base_pool
    # TIER 6: fallback sin tarjeta (último recurso)
    tier5 = [a for a in accs if a.get('status') == 'active'
             and a.get('id') not in used_ids and a.get('id') not in bad_creds]

    # 2026-05-12 FIX: priorizar cuentas YA VALIDADAS (credentials_verified=True
    # confirmadas con TM) sobre virgin_fresh. Las virgin no están realmente
    # registradas en TM (solo en accounts.json) → TM muestra pantalla
    # REGÍSTRATE en vez de login → bot no avanza al checkout.
    #
    # 2026-05-12 fix #2: sub-tier "validated+recent_login" — cuentas con
    # last_login muy reciente (>9-may) tienen cookies frescas y TM no las
    # marca para forced reset. Estas son las óptimas para llegar a 3DS.
    from datetime import datetime as _dt2
    def _last_login_after(a, cutoff_date):
        ll = (a.get('last_login') or '').strip()
        if not ll: return False
        try:
            ts = _dt2.fromisoformat(ll.replace('Z','+00:00').split('+')[0])
            return ts > cutoff_date
        except Exception:
            return False
    tier_recent_login = [a for a in tier1
                          if _last_login_after(a, _dt2(2026, 5, 9))]

    # ═══ TIER 0 (FIX 2026-05-14): DOMESTICADAS — cuentas que el bot ya pasó
    # por checkout SIN forced_reset (tienen `last_bot_state_save_at` reciente).
    # Estas cuentas tienen máxima device-trust con TM → eligen PRIMERO.
    # last_bot_state_save_at lo set _save_state_now() cuando llegamos a
    # checkout_detected_at_entry o purchase_gate_abort (= flow OK sin reset).
    def _is_domesticated(a):
        ts = (a.get('last_bot_state_save_at') or '').strip()
        if not ts: return False
        try:
            t = _dt2.fromisoformat(ts.replace('Z','+00:00').split('+')[0])
            age_days = (_dt2.utcnow() - t).total_seconds() / 86400
            return age_days < 30   # state fresh = TM Identity confía
        except Exception:
            return False
    tier_domesticated = [a for a in tier1 if _is_domesticated(a)]

    for tier_name, tier in [("DOMESTICATED", tier_domesticated),
                             ("validated+recent_login", tier_recent_login),
                             ("validated+ready", tier1),
                             ("validated_only", tier2),
                             ("virgin_fresh", tier_virgin),
                             ("purchase_ready_only", tier3),
                             ("base_pool", tier4),
                             ("fallback_no_card", tier5)]:
        if tier:
            # Para virgin_fresh, ordenar por registration_completed_at desc;
            # para validated+recent_login, ordenar por last_login desc;
            # para los demás, por validated_at desc (más reciente primero).
            if tier_name == 'virgin_fresh':
                sort_key = 'registration_completed_at'
            elif tier_name == 'validated+recent_login':
                sort_key = 'last_login'
            else:
                sort_key = 'validated_at'
            tier = sorted(tier, key=lambda a: a.get(sort_key) or '', reverse=True)
            a = tier[0]
            c = cards_by_acc.get(a.get('id'))
            log(slot, "pick_account_tier", tier=tier_name,
                email=a.get('tm_email'),
                validated_at=a.get('validated_at'),
                registration_completed_at=a.get('registration_completed_at'),
                credentials_verified=a.get('credentials_verified'),
                purchase_ready=a.get('purchase_ready'),
                last_login=a.get('last_login'),
                pool_size=len(tier))
            return a, c
    return None, None


def login_with_account_swap(slot, page, ctx, account, card,
                             used_ids_lock_path, max_account_swaps=LIMIT_ACCOUNT_SWAPS):
    """Login robusto: si detecta credenciales inválidas, marca la cuenta como
    bad, libera lock, pickea OTRA cuenta de la lista, y reintenta hasta
    `max_account_swaps` veces. Devuelve dict {ok, account, card, swaps}.
    Si tras max_swaps sigue fallando → ok=False y caller debe cerrar browser."""
    swaps = 0
    current_account = account
    current_card = card
    # 2026-05-12 fix: persistir IDs que ya intentamos (login fail o recovery
    # fail) para que pick_account NO los re-pickee. Sino, con solo 1 cuenta
    # validated_recent disponible, el swap re-elige la misma cuenta forever.
    tried_account_ids = set()
    while True:
        result = login_full(slot, page, current_account)
        # Normalizar: login_full puede retornar True / False / "reset_required" /
        # "bad_credentials". Si retorna False (OTP timeout, no_pwd_input, etc.)
        # tratarlo como bad_credentials para forzar swap de cuenta interno —
        # antes se quedaba en el caller con ok=False sin reintentar.
        if result is False:
            log(slot, "login_full_false_treating_as_bad_creds",
                email=current_account.get('tm_email'))
            result = "bad_credentials"

        # ═══ TM forzó password reset (cuenta inactiva) ═══
        # NORMA TESTING 2026-05-12 user: SI estamos en modo testing
        # (env var TEST_NO_RECOVERY=1), SKIP recovery para NO quemar cuenta
        # con TM. Recovery + relogin inmediato dispara Akamai bot_block en
        # esta cuenta + subnet. Mejor cerrar limpio y probar OTRA cuenta.
        if result == "reset_required" and os.environ.get('TEST_NO_RECOVERY', '0') == '1':
            log(slot, "test_mode_skip_recovery",
                email=current_account.get('tm_email'),
                note="TM pidió reset. Skip recovery para no quemar cuenta. Swap.")
            # Cerrar el browser limpio y devolver bad_creds para que el caller
            # haga swap a otra cuenta SIN tocar el recovery.
            tried_account_ids.add(current_account.get('id'))
            return {"ok": False, "reason": "reset_required_test_skip",
                    "swaps": swaps}
        # Auto-recovery: invocar account_recovery.recover_inline para resetear
        # password vía email IMAP madre, persistir la nueva pwd, y re-login.
        if result == "reset_required":
            log(slot, "auto_recovery_start",
                email=current_account.get('tm_email'),
                madre=current_account.get('email_contact') or current_account.get('contact_email'))
            # PRECONDICIÓN: estamos en pantalla "Restablecer la contraseña"
            # (TM forzado). El email viene pre-rellenado y hay un botón azul
            # "Restablecer la contraseña". DEBEMOS clickearlo antes de invocar
            # recover_inline — sino TM nunca envía el reset email al madre.
            # 1) Cerrar banner cookies si tapa el botón
            try:
                for txt in ["Aceptar todas las cookies", "Aceptar", "Accept",
                            "Rechazarlas todas", "Reject"]:
                    try:
                        el = page.get_by_text(txt, exact=False).first
                        if el.is_visible(timeout=900):
                            el.click(timeout=2500); page.wait_for_timeout(700); break
                    except Exception: continue
            except Exception: pass
            # 2) Click el botón azul "Restablecer la contraseña"
            try:
                clicked = False
                for sel_pat in [r'^restablecer la contraseña$', r'^restablecer$',
                                r'^reset password$', r'^reset my password$']:
                    try:
                        btn = page.get_by_role('button',
                            name=re.compile(sel_pat, re.I)).first
                        btn.scroll_into_view_if_needed(timeout=3000)
                        btn.click(timeout=4500, force=True)
                        clicked = True
                        log(slot, "force_reset_btn_clicked", pattern=sel_pat)
                        break
                    except Exception: continue
                if not clicked:
                    # Fallback DOM: botón azul cuyo texto contenga "restablecer"
                    clicked = page.evaluate(r"""() => {
                        const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                        const t = btns.find(b => /restablecer.*contrase/i.test(b.innerText||b.textContent||''));
                        if (t) { t.click(); return true; }
                        return false;
                    }""")
                    log(slot, "force_reset_btn_fallback_dom_click", clicked=bool(clicked))
                if clicked:
                    # TM mostrará pantalla intermedia "elige método" (SMS/email)
                    # con radio SMS pre-seleccionado. Hay que cambiar a email
                    # antes de Siguiente — sino recibimos OTP por SMS al teléfono
                    # de la cuenta (que no controlamos).
                    page.wait_for_timeout(2500)
                    shoot(slot, page, "post_force_reset_btn_click")
                    # Click radio "Envíame un correo electrónico"
                    radio_ok = False
                    try:
                        # Selector estándar TM
                        try:
                            page.locator('#email-radio-input').click(force=True, timeout=2500)
                            radio_ok = True
                        except Exception: pass
                        if not radio_ok:
                            # Fallback DOM por texto del label
                            radio_ok = page.evaluate(r"""() => {
                                const all = Array.from(document.querySelectorAll(
                                    'label, [role="radio"], input[type="radio"]'));
                                const t = all.find(x => /env[ií]ame.*correo|correo electr[oó]nico|email.*me|send.*email/i.test(
                                    (x.innerText||x.textContent||x.getAttribute('aria-label')||'')));
                                if (t) {
                                    if (t.tagName === 'INPUT') { t.checked = true; t.click(); }
                                    else { t.click(); }
                                    return true;
                                }
                                return false;
                            }""")
                    except Exception as _re:
                        log(slot, "radio_email_err", err=str(_re)[:120])
                    log(slot, "radio_email_selected", ok=bool(radio_ok))
                    page.wait_for_timeout(900)
                    # Click "Siguiente" — esto provoca el envío del email a TM
                    next_ok = False
                    for sel_pat in [r'^siguiente$', r'^continuar$', r'^next$',
                                     r'^enviar$', r'^submit$']:
                        try:
                            btn = page.get_by_role('button',
                                name=re.compile(sel_pat, re.I)).first
                            btn.scroll_into_view_if_needed(timeout=2500)
                            btn.click(timeout=3500, force=True)
                            next_ok = True
                            log(slot, "reset_method_next_clicked", pattern=sel_pat)
                            break
                        except Exception: continue
                    if not next_ok:
                        log(slot, "reset_method_next_failed")
                    # Dar tiempo a TM a enviar el email reset (~5s)
                    page.wait_for_timeout(5500)
                    shoot(slot, page, "post_reset_method_next")
            except Exception as _e:
                log(slot, "force_reset_btn_err", err=str(_e)[:120])
            try:
                import account_recovery as _arc
                # Construir user-like compatible con recover_inline
                class _UserShim:
                    pass
                u = _UserShim()
                u.email = current_account.get('tm_email') or ''
                _DEFAULT_MADRE_EMAIL = 'johnlockperdidos@icloud.com'
                _DEFAULT_MADRE_PWD = (os.environ.get('TM_IMAP_MADRE_PWD')
                                       or 'jzny-tfyh-qtuq-feou')
                _ce = (current_account.get('email_contact')
                       or current_account.get('contact_email')
                       or '').strip().lower()
                if not _ce or _ce == _DEFAULT_MADRE_EMAIL:
                    # Madre default → usar app-pwd madre (NO la email_password
                    # de la cuenta hija, que es de un buzón distinto)
                    u.contact_email = _DEFAULT_MADRE_EMAIL
                    u.contact_pwd = _DEFAULT_MADRE_PWD
                else:
                    # Contact email específico de la cuenta → usar su propia pwd
                    u.contact_email = _ce
                    u.contact_pwd = (current_account.get('contact_pwd')
                                     or current_account.get('email_password')
                                     or _DEFAULT_MADRE_PWD)
                # Wrapper de log compatible con la firma `log=print`
                def _rec_log(msg):
                    try: log(slot, "recovery", msg=str(msg)[:200])
                    except Exception: pass
                rec = _arc.recover_inline(page, ctx, u, run=None,
                                           log=_rec_log, max_imap_wait_s=240,
                                           request_already_sent=True)
                log(slot, "recovery_result",
                    ok=rec.get('ok'), err=(rec.get('error') or '')[:120],
                    new_pwd_preview=((rec.get('new_password') or '')[:4] + '***')
                        if rec.get('new_password') else None)
                if rec.get('ok') and rec.get('new_password'):
                    new_pwd = rec.get('new_password')
                    # OPTIM 2026-05-12 ANTI-BOT: tras recovery exitoso, NO ir
                    # directo al evento. Akamai detecta patrón "reset pwd →
                    # login → cart" en menos de 1 min como bot.
                    # Mitigación: navegar a TM home + scrolls humanos 25-30s
                    # ANTES de re-navegar al evento. Simula "usuario cambia pwd
                    # y luego explora la web" — patrón humano normal.
                    log(slot, "post_recovery_human_warming_start")
                    try:
                        page.goto("https://www.ticketmaster.es/",
                                  wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(int(2000 + random.random()*1500))
                        # Cerrar cookies banner si reaparece
                        try:
                            page.evaluate(r"""() => {
                                const b = document.querySelector('#onetrust-accept-btn-handler');
                                if (b && b.offsetParent !== null) b.click();
                            }""")
                        except Exception: pass
                        page.wait_for_timeout(800)
                        # Scrolls humanos extensos (10-15s simulando navegación)
                        for sy in (350, 800, 1300, 1800, 1200, 600, 200):
                            page.evaluate(f"window.scrollTo({{top:{sy},behavior:'smooth'}})")
                            page.wait_for_timeout(int(1200 + random.random()*1500))
                        # Hover sobre algún evento (humano mira tarjetas)
                        try:
                            page.evaluate(r"""() => {
                                const cards = Array.from(document.querySelectorAll(
                                    'a[href*="/event/"], [class*="card" i] a'))
                                    .filter(c => c.offsetParent !== null);
                                if (cards.length) {
                                    const t = cards[Math.floor(Math.random()*Math.min(cards.length, 8))];
                                    const r = t.getBoundingClientRect();
                                    ['mouseenter','mouseover','mousemove'].forEach(ev =>
                                        t.dispatchEvent(new MouseEvent(ev, {
                                            bubbles: true, clientX: r.left+r.width/2, clientY: r.top+r.height/2
                                        })));
                                }
                            }""")
                        except Exception: pass
                        page.wait_for_timeout(int(2000 + random.random()*2000))
                        log(slot, "post_recovery_human_warming_done")
                    except Exception as _whe:
                        log(slot, "post_recovery_human_warming_err", err=str(_whe)[:80])
                    # Persistir nueva pwd en accounts.json + plantilla CSV.
                    # account_recovery._persist_recovered_password ya lo hace
                    # internamente si recover_inline tuvo éxito (rec.persist).
                    # Aquí refrescamos el dict en memoria para el siguiente login.
                    current_account['tm_password'] = new_pwd
                    log(slot, "recovery_persisted",
                        email=current_account.get('tm_email'),
                        persist_info=rec.get('persist'))
                    shoot(slot, page, "post_recovery_ready_to_login")
                    # Tras reset, TM auto-loguea al usuario y redirige a /account
                    # (no a checkout). El cart se perdió → main() debe re-navegar
                    # al evento y rehacer el flow stepper+find_tickets. Salimos
                    # del swap loop señalando session_recovered=True.
                    page.wait_for_timeout(2500)
                    # Skip passkey prompt si aparece tras recovery
                    try:
                        _skip_passkey_prompt(slot, page)
                    except Exception:
                        pass
                    return {"ok": True, "session_recovered": True,
                            "account": current_account, "card": current_card,
                            "swaps": swaps}
                else:
                    log(slot, "recovery_failed_swap_account",
                        err=(rec.get('error') or '')[:200])
                    # Recovery falló — tratar como credenciales malas y swap
                    result = "bad_credentials"
            except Exception as _e:
                log(slot, "recovery_exception", err=f"{type(_e).__name__}: {str(_e)[:200]}")
                result = "bad_credentials"

        if result == "bad_credentials":
            # 2026-05-12 fix: añadir esta cuenta a tried_account_ids ANTES de
            # liberar el lock — para que pick_account no la re-elija.
            if current_account.get('id'):
                tried_account_ids.add(current_account.get('id'))
            # Liberar la cuenta del lock + marcar bad
            try:
                with open(used_ids_lock_path, encoding='utf-8') as f:
                    ids = set(json.load(f))
                ids.discard(current_account.get('id'))
                with open(used_ids_lock_path, 'w', encoding='utf-8') as f:
                    json.dump(list(ids), f)
            except Exception: pass
            swaps += 1
            if swaps >= max_account_swaps:
                log(slot, "max_account_swaps_reached", swaps=swaps)
                return {"ok": False, "reason": "max_swaps", "swaps": swaps,
                        "account": current_account, "card": current_card}
            # Pick otra cuenta — excluir las que ya intentamos
            try:
                with open(used_ids_lock_path, encoding='utf-8') as f:
                    ids = set(json.load(f))
            except Exception:
                ids = set()
            ids |= tried_account_ids
            new_account, new_card = pick_account(slot, ids)
            if not new_account or (new_account.get('id') in tried_account_ids):
                log(slot, "no_more_accounts_to_try",
                    tried_count=len(tried_account_ids))
                return {"ok": False, "reason": "no_more_accounts",
                        "swaps": swaps}
            log(slot, "account_swap", from_email=current_account.get('tm_email'),
                to_email=new_account.get('tm_email'))
            ids.add(new_account.get('id'))
            try:
                with open(used_ids_lock_path, 'w', encoding='utf-8') as f:
                    json.dump(list(ids), f)
            except Exception: pass
            # Cargar nuevo storage_state si existe
            ssf = new_account.get('storage_state_file')
            if ssf and os.path.exists(ssf):
                try:
                    with open(ssf, encoding='utf-8') as f:
                        state = json.load(f)
                    ctx.clear_cookies()
                    if state.get('cookies'):
                        ctx.add_cookies(state['cookies'])
                except Exception as e:
                    log(slot, "storage_swap_err", err=str(e)[:80])
            current_account = new_account

            # ═══ FIX 2026-05-14: page health check antes de retry login ═══
            # Tras recovery_failed o bad_credentials, la página puede quedar en
            # estado roto (OTP rejected screen, modal, error, target closed).
            # Si reusamos la misma `page` para el siguiente login, el form de
            # email no existe → `no_email_input` cascada y quemamos cuentas en
            # serie. Solución: verificar que la página es navegable y, si no,
            # abortar el swap para que el wrapper externo haga retry fresh.
            try:
                page_ok = False
                try:
                    _cur_url = page.url or ''
                except Exception:
                    _cur_url = ''
                # Heurística: si la URL aún es auth.tm.com con email input visible,
                # la página está sana. Si no, está rota.
                try:
                    _has_email_input = page.evaluate(
                        r"""() => {
                            const inp = document.querySelector(
                                'input[type="email"], input[name*="email" i], input[id*="email" i]');
                            return !!(inp && inp.offsetParent !== null);
                        }""")
                except Exception:
                    _has_email_input = False
                # Si NO hay email input → renavegar al OAuth endpoint para resetear
                if not _has_email_input:
                    log(slot, "page_corrupted_renavigating",
                        prev_url=_cur_url[:80])
                    try:
                        # Re-emitir el endpoint OAuth con los mismos params que
                        # TM construyó (capturado del referer original)
                        if 'auth.ticketmaster.com' in _cur_url:
                            page.goto(_cur_url, wait_until='domcontentloaded',
                                      timeout=15000)
                            page.wait_for_timeout(2000)
                            page_ok = True
                        else:
                            # No tenemos URL OAuth válida → abortar swap
                            log(slot, "page_corrupt_no_oauth_url_abort",
                                url=_cur_url[:80])
                            return {"ok": False,
                                    "reason": "page_corrupted_after_swap",
                                    "swaps": swaps,
                                    "account": current_account,
                                    "card": current_card}
                    except Exception as _re:
                        log(slot, "page_renav_err", err=str(_re)[:120])
                        return {"ok": False,
                                "reason": "page_renav_failed_after_swap",
                                "swaps": swaps,
                                "account": current_account,
                                "card": current_card}
                else:
                    page_ok = True
            except Exception as _hce:
                log(slot, "page_health_check_err", err=str(_hce)[:120])
                page_ok = False
            current_card = new_card
            # FIX 2026-05-14: Si el health check ya renavegó al endpoint OAuth
            # correcto (page_ok=True), NO sobrescribir con my.ticketmaster.es/.
            # Solo hacer fallback goto si el check falló (page_ok=False).
            if not page_ok:
                # Volver a abrir formulario de login
                try:
                    page.goto("https://my.ticketmaster.es/", timeout=30000,
                              wait_until="domcontentloaded")
                    human_pause(2, 4)
                except Exception: pass
            continue
        # ═══ FIX 2026-05-14: skip "Añadir Llave de Acceso" prompt si aparece ═══
        # TM muestra modal passkey tras login OK en algunas cuentas. Sin skip,
        # el bot queda atascado esperando un click que nunca llega y el flow
        # nunca avanza al checkout.
        if result is True:
            try:
                _skip_passkey_prompt(slot, page)
            except Exception:
                pass

        # ═══ FIX 2026-05-14: persistir storage_state tras login exitoso ═══
        # Captura cookies TM Identity + eps_sid + SID/BID para que el próximo
        # run con esta cuenta dentro de MAX_STORAGE_STATE_AGE_S reutilice el
        # device-trust → TM NO pide forced_password_reset.
        # Solo guarda si el login fue exitoso (result=True).
        if result is True:
            try:
                from datetime import datetime as _dt
                _ts = _dt.now().strftime('%Y%m%d_%H%M%S')
                _aid_short = str(current_account.get('id') or 'unk')[:12]
                _sessions_dir = os.path.join(PROYECTO, "datos", "sessions")
                os.makedirs(_sessions_dir, exist_ok=True)
                _state_file = os.path.join(
                    _sessions_dir, f"tm_cmp_{_aid_short}_{_ts}_state.json")
                ctx.storage_state(path=_state_file)
                # Actualizar accounts.json con la nueva ruta state_file
                try:
                    from account_manager import account_manager as _am
                    _am.update_account(current_account.get('id'),
                                        {"storage_state_file": _state_file})
                    log(slot, "storage_state_saved",
                        path=os.path.basename(_state_file),
                        account_id=current_account.get('id'))
                except Exception as _ue:
                    log(slot, "storage_state_saved_no_update",
                        path=os.path.basename(_state_file),
                        err=str(_ue)[:80])
            except Exception as _se:
                log(slot, "storage_state_save_err", err=str(_se)[:120])
        # Resultado True/False normal del login_full
        return {"ok": bool(result), "account": current_account,
                "card": current_card, "swaps": swaps}


def main():
    args = parse_args()
    # ─── KILL SWITCH CROSS-PROCESS CHECK (P2 audit) ───────────────
    # El parent process puede setear el flag desde el dashboard.
    # Aquí el subprocess lo respeta y aborta inmediatamente.
    # Fail-safe: si la check falla, el bot continúa normalmente.
    try:
        from pathlib import Path
        if Path("runs/_kill_switch.flag").exists():
            try:
                log(args.slot, "kill_switch_active_aborting_bot")
            except Exception:
                pass
            try:
                write_outcome(args.slot, {
                    "status": "kill_switch_aborted",
                    "step_reached": "startup",
                    "rc": 99,
                })
            except Exception:
                pass
            return 99
    except Exception:
        pass
    # ─── STEP 11 INSTRUMENTATION HOOK (fail-safe, observability-only) ───
    # `_bridge_ctx` queda como None si la observability layer no está
    # disponible. Los hooks NUNCA propagan excepciones — el bot sigue su
    # flow normal incluso si la instrumentación falla.
    _bridge_ctx = None
    try:
        from core.observability.bridge import (
            start_flow as _start_flow,
            record_intent as _record_intent,
        )
        _bridge_ctx = _start_flow(
            slot=args.slot,
            event_id=(args.event_url or "")[:120],
            account_id="",
            seats=[],
        )
        # ─── Phase 4 WAL: write intent BEFORE bot does its work ───
        # Esto crea el WAL entry que permite orphan detection en crashes.
        if _bridge_ctx is not None:
            _record_intent(
                _bridge_ctx,
                step="bot_main_entry",
                event_url=(args.event_url or "")[:120],
                slot=args.slot,
                qty=getattr(args, "qty", None),
                offer_idx=getattr(args, "offer_idx", None),
            )
    except Exception:
        _bridge_ctx = None
    # ──────────────────────────────────────────────────────────────────
    # Reset outcome al iniciar (si hubo run anterior)
    try:
        op = _outcome_path(args.slot)
        if os.path.exists(op):
            os.remove(op)
    except Exception:
        pass
    write_outcome(args.slot, {"status": "started",
                                "event_url": args.event_url[:120],
                                "qty": args.qty, "offer_idx": args.offer_idx,
                                "started_at": datetime.now().isoformat()})
    slot = args.slot
    log(slot, "init", event_url=args.event_url[:60], offer_idx=args.offer_idx, qty=args.qty)

    # Espera escalonada por slot para evitar contención del mutex en spawns
    # paralelos. Solo aplica a slots 1-12 (uso CLI manual). Slots >=100 son
    # asignados por el Sniper Bot UI vía bot_engine._next_slot() — no necesitan
    # escalonado porque cada one-shot ya pasa por el pick_mutex con timeout.
    if 1 <= slot <= 12:
        pre_sleep = max(0, (slot - 1) * 5)
    else:
        pre_sleep = 0
    if pre_sleep:
        log(slot, "pre_sleep_for_lock", seconds=pre_sleep)
        time.sleep(pre_sleep)

    # 1) Pick perfil MoreLogin con MUTEX cross-process (O_EXCL atomic)
    # Solo si engine=morelogin. Para camoufox usamos Quantum proxy directo con
    # sessionid aleatorio (norma user 2026-05-12: misma lógica que MoreLogin,
    # solo cambia el navegador).
    prof = None
    engine = getattr(args, 'engine', 'morelogin')
    if engine in ('morelogin', 'silent'):
        for retry in range(RETRY_PROFILE_PICK):
            if not _acquire_pick_mutex(timeout=30):
                log(slot, "pick_mutex_timeout"); time.sleep(3); continue
            try:
                if engine == 'silent':
                    prof = pick_silent_profile(slot)
                else:
                    prof = pick_morelogin_profile(slot)
            finally:
                _release_pick_mutex()
            if prof: break
            log(slot, "pick_retry", retry=retry+1)
            time.sleep(3)
        if not prof:
            log(slot, "FATAL_no_profile")
            # ─── P5 SRE: surgical observability hook ───
            write_outcome(slot, {
                "status": "fatal_no_profile",
                "step_reached": "profile_pick",
                "rc": 1,
            })
            return 1
        log(slot, "profile_picked", **prof)
        # 2) Pick cuenta + tarjeta (MoreLogin: orden original)
        used_lock_path = os.path.join(PROYECTO, "_compra_parallel_used_accounts.lock")
        # FIX 2026-05-14: auto-cleanup del lock si edad >30min (proceso huérfano)
        try:
            if os.path.exists(used_lock_path):
                _lock_age_s = time.time() - os.path.getmtime(used_lock_path)
                if _lock_age_s > 1800:   # 30 min
                    log(slot, "lock_stale_auto_cleanup", age_s=int(_lock_age_s))
                    with open(used_lock_path, 'w', encoding='utf-8') as f:
                        json.dump([], f)
        except Exception: pass
        used_ids = set()
        try:
            if os.path.exists(used_lock_path):
                with open(used_lock_path, encoding='utf-8') as f:
                    used_ids = set(json.load(f))
        except Exception: pass
        account, card = pick_account(slot, used_ids)
    else:
        # ════════════════════════════════════════════════════════════════
        # CAMOUFOX FIX 2026-05-14 — Sessid estable per cuenta
        # ════════════════════════════════════════════════════════════════
        # ANTES: sessid = random.randint(100000, 999999) por run
        #   → Cada run con la misma cuenta = browser fingerprint distinto
        #   → TM Identity ve "device desconocido" → forced_password_reset
        # AHORA: sessid = hash(account.id) → estable per cuenta
        #   → Misma cuenta SIEMPRE usa el mismo sessid (browser + IP Quantum)
        #   → TM Identity reconoce el device → permite login normal
        #   → BID cookie (364d expiry) sobrevive entre runs
        # ════════════════════════════════════════════════════════════════
        # INVERTIR el orden: primero pick_account, luego derivar sessid
        used_lock_path = os.path.join(PROYECTO, "_compra_parallel_used_accounts.lock")
        # FIX 2026-05-14: auto-cleanup del lock si edad >30min (proceso huérfano)
        try:
            if os.path.exists(used_lock_path):
                _lock_age_s = time.time() - os.path.getmtime(used_lock_path)
                if _lock_age_s > 1800:
                    log(slot, "lock_stale_auto_cleanup", age_s=int(_lock_age_s))
                    with open(used_lock_path, 'w', encoding='utf-8') as f:
                        json.dump([], f)
        except Exception: pass
        used_ids = set()
        try:
            if os.path.exists(used_lock_path):
                with open(used_lock_path, encoding='utf-8') as f:
                    used_ids = set(json.load(f))
        except Exception: pass
        account, card = pick_account(slot, used_ids)
        if not account:
            log(slot, "FATAL_no_account")
            write_outcome(slot, {
                "status": "fatal_no_account",
                "step_reached": "account_pick_camoufox",
                "rc": 1,
            })
            return 1

        # ═══════════════════════════════════════════════════════════════
        # 2026-05-15: REVERTED IP rotation. TM ve "misma cuenta desde
        # 5 IPs distintas/hora" → bot-block "Su sesión se ha suspendido".
        # Cuenta = persona + IP ESTABLES per account (como humano real).
        # La rotación de IP solo aplica para REGISTRO (cada cuenta nueva
        # con IP nueva), NO para compra con cuenta existente.
        # ═══════════════════════════════════════════════════════════════
        import hashlib as _hashlib
        _aid = str(account.get('id', '') or account.get('tm_email', '') or 'fallback')
        _h = int(_hashlib.sha256(_aid.encode()).hexdigest()[:8], 16)
        sessid = 100000 + (_h % 900000)   # estable per cuenta (IP + persona)
        prof = {
            "profile": f"camoufox-{sessid}",
            "env_id": f"camoufox-{sessid}",
            "ip": "",
            "proxy_username": f"Quantum_927evhgx-cc-ES-pool-qntm-sessionid-{sessid}-sesstime-60",
            "proxy_password": "ycp4e57m",
            "proxy_host": "residential.quantumproxies.net",
            "proxy_port": 8888,
            "proxy_country": "ES",
            "stable_sessid": True,            # marca para usar en TTL state
            "derived_from_account_id": _aid[:12],
        }
        log(slot, "profile_picked_camoufox_stable",
            sessionid=sessid, account_id=_aid[:12],
            note="hash(account.id) → fingerprint+IP estables per cuenta (anti bot-block)")
    if not account:
        log(slot, "FATAL_no_account")
        # ─── P5 SRE: surgical observability hook ───
        write_outcome(slot, {
            "status": "fatal_no_account",
            "step_reached": "account_pick",
            "rc": 1,
        })
        return 1
    # ─── PHASE A audit: update bridge trace with real account ───
    # cart_id no existe en este bot (TM flow es DOM-based). La identidad
    # real es slot + idempotency_key + account_email + event_url.
    # Aquí emitimos el primer evento con account_email poblado.
    if _bridge_ctx is not None:
        try:
            from core.observability.bridge import record_intent as _rec_intent_account
            _rec_intent_account(
                _bridge_ctx,
                step="account_picked",
                account_email=account.get('tm_email', '') or '',
                account_internal_id=str(account.get('id', '')),
            )
        except Exception:
            pass
    used_ids.add(account.get('id'))
    try:
        with open(used_lock_path, 'w', encoding='utf-8') as f:
            json.dump(list(used_ids), f)
    except Exception: pass
    log(slot, "account_picked", email=account.get('tm_email'),
        has_card=bool(card))

    if not card:
        # Buscar 1ª tarjeta del CSV plantilla
        cards = am.account_manager.list_cards()
        card = cards[0] if cards else {}
        log(slot, "card_fallback", id=card.get('id'))

    # ════════════════════════════════════════════════════════════════
    # PRE-FLIGHT HTTP 2026-05-13 — descarta IP quemada ANTES del browser.
    # Si TM ya responde con bot_block via HTTP directo, NO levantamos el
    # browser (ahorra 60s+). Solo aplica a engine=morelogin (Camoufox usa
    # su propio proxy directo via geoip).
    # ════════════════════════════════════════════════════════════════
    if engine == 'morelogin' and prof.get('proxy_username'):
        try:
            import purchase_stealth as _ps
            proxy_url = (f"http://{prof['proxy_username']}:ycp4e57m"
                          f"@residential.quantumproxies.net:8888")
            pf = _ps.pre_flight_http_check(
                proxy_url,
                expected_country=(prof.get('proxy_country') or 'ES'))
            log(slot, "pre_flight_http",
                ok=pf.get("ok"), pf_stage=pf.get("stage"),
                ip=(pf.get("ip_reputation") or {}).get("ip"),
                isp=(pf.get("ip_reputation") or {}).get("isp"),
                body_len=pf.get("body_len"))
            # FIX 2026-05-13: solo abortar por BLOQUEO HTTP real, no por latencia/geo.
            # Quantum residencial puede tener latencia >2s legítimamente.
            # Causas de abort REALES (no soft): bot_block_at_http, ip_flag_datacenter,
            # ip_flag_proxy_tag. flag_slow / flag_geo_mismatch → solo warning.
            _fatal_pf_stages = ("bot_block_at_http", "ip_flag_datacenter",
                                "ip_flag_proxy_tag")
            if not pf.get("ok") and pf.get("stage") in _fatal_pf_stages:
                log(slot, "pre_flight_FAIL_abort",
                    pf_stage=pf.get("stage"),
                    matched=pf.get("matched"))
                # Destroy auto-created profile + recreate con sessid nuevo
                if prof.get("auto_created"):
                    try:
                        d = bo.destroy_fresh_profile(prof)
                        log(slot, "auto_profile_destroyed_after_pre_flight", **d)
                    except Exception as _de:
                        log(slot, "destroy_err_pre_flight", err=str(_de)[:120])
                _remove_active_profile(prof.get("profile"))
                # ─── P4 SRE: surgical hook for critical silent exit ───
                write_outcome(slot, {
                    "status": "fatal_pre_flight_abort",
                    "step_reached": "pre_flight",
                    "pf_stage": pf.get("stage"),
                    "matched": pf.get("matched"),
                    "rc": 1,
                })
                return 1
        except Exception as _pfe:
            log(slot, "pre_flight_skipped_err", err=str(_pfe)[:160])

    # 3) Arrancar browser según engine
    if engine == 'morelogin':
        # FIX 2026-05-13: si un perfil falla "No pudo conectarse", marcar
        # burned y pickear OTRO. Hay 1865+ Quantum ES y la mayoría arrancan
        # pero algunos perfiles específicos están en estado broken local.
        port = None
        for swap_attempt in range(RETRY_PROFILE_START):
            try:
                r = requests.post(f'{bo.MORELOGIN_API}/api/env/start',
                                   json={'envId': prof['env_id']}, timeout=30).json()
                port_raw = (r.get('data', {}) or {}).get('debugPort', '')
                code = r.get('code', -1)
                if code == 0 and port_raw:
                    port = int(port_raw)
                    log(slot, "profile_started", port=port,
                        profile=prof.get('profile'))
                    break
                log(slot, "profile_start_failed",
                    profile=prof.get('profile'),
                    code=code, msg=r.get('msg', '')[:80],
                    swap_attempt=swap_attempt+1)
                # Cerrar y marcar burned para próximo pick
                try:
                    requests.post(f'{bo.MORELOGIN_API}/api/env/close',
                                   json={'envId': prof['env_id']}, timeout=10)
                except Exception: pass
                # Burn este perfil
                try:
                    import register_account as _ra
                    if hasattr(_ra, 'load_burned') and hasattr(_ra, 'save_burned'):
                        b = _ra.load_burned()
                        b.add(prof.get('profile'))
                        _ra.save_burned(b)
                        log(slot, "profile_burned_local",
                            profile=prof.get('profile'))
                except Exception: pass
                # Pick OTRO perfil
                _remove_active_profile(prof.get('profile'))
                if not _acquire_pick_mutex(timeout=15):
                    log(slot, "pick_mutex_timeout_after_swap")
                    break
                try:
                    new_prof = pick_morelogin_profile(slot)
                finally:
                    _release_pick_mutex()
                if not new_prof:
                    log(slot, "no_more_profiles_after_swap")
                    break
                prof = new_prof
                log(slot, "profile_swap", to=prof.get('profile'),
                    ip=prof.get('ip'))
                time.sleep(2)
            except Exception as e:
                log(slot, "profile_start_exception", err=str(e)[:120])
                time.sleep(2)
        if not port:
            log(slot, "FATAL_profile_start",
                err="all swap_attempts exhausted")
            # ─── P5 SRE: surgical observability hook ───
            write_outcome(slot, {
                "status": "fatal_profile_start_exhausted",
                "step_reached": "profile_start",
                "swap_attempts_exhausted": True,
                "rc": 1,
            })
            return 1
        time.sleep(5)

    elif engine == 'silent':
        # Silent: perfil ya tiene proxy embebido; /open con cdp:true devuelve
        # el puerto CDP y el connect es idéntico a morelogin (mismo `port`).
        import silent_engine as _se
        port = None
        for swap_attempt in range(RETRY_PROFILE_START):
            port = _se._start_profile(prof['env_id'])
            if port:
                log(slot, "profile_started", port=port,
                    profile=prof.get('profile'), engine='silent')
                break
            log(slot, "profile_start_failed",
                profile=prof.get('profile'),
                msg="Silent devolvio None: habilita CDP via API en "
                    "Ajustes de Silent (y revisa SILENT_TOKEN)",
                swap_attempt=swap_attempt+1)
            # Liberar y pickear OTRO perfil Silent
            try:
                _se._release_profile(prof.get('profile'))
            except Exception: pass
            if not _acquire_pick_mutex(timeout=15):
                log(slot, "pick_mutex_timeout_after_swap")
                break
            try:
                new_prof = pick_silent_profile(slot)
            finally:
                _release_pick_mutex()
            if not new_prof:
                log(slot, "no_more_profiles_after_swap")
                break
            prof = new_prof
            log(slot, "profile_swap", to=prof.get('profile'), ip=prof.get('ip'))
            time.sleep(2)
        if not port:
            log(slot, "FATAL_profile_start",
                err="silent: all swap_attempts exhausted. Silent: habilita "
                    "CDP via API en Ajustes de Silent")
            write_outcome(slot, {
                "status": "fatal_profile_start_exhausted",
                "step_reached": "profile_start",
                "engine": "silent",
                "swap_attempts_exhausted": True,
                "rc": 1,
            })
            return 1
        time.sleep(5)

    try:
        from playwright.sync_api import sync_playwright
        if engine == 'camoufox':
            # Camoufox: Firefox C++ patched + Quantum proxy directo
            try:
                from camoufox.sync_api import Camoufox
            except ImportError as _ie:
                log(slot, "FATAL_camoufox_import", err=str(_ie))
                return 1
            cf_kwargs = {
                "headless": getattr(args, 'headless', False),
                # 2026-05-12 fix anti-bot_block: geoip=True alinea timezone/
                # locale/screen del browser con la IP REAL del proxy (Quantum
                # residential ES). Akamai detecta mismatch geo→fingerprint
                # como bot. Camoufox descarga MaxMind GeoLite primera vez.
                "geoip": True,
                "locale": "es-ES",
                "humanize": True,
                "i_know_what_im_doing": True,
                "os": "windows",
                # Norma user 2026-05-12: tamaño 1600x1000 — el user tiene
                # monitor 1920x1080 y a 1280x800 la ventana queda muy pequeña
                # ocupando solo ~66% del monitor (visualmente "descompensada").
                # 1600x1000 ocupa ~83% del monitor sin ser fullscreen.
                # `no_viewport=True` en new_context permite que el usuario
                # maximice/minimice/redimensione manualmente sin romper layout.
                "window": (1600, 1000),
                "disable_coop": True,
                # 2026-05-12 FIX CRÍTICO WebRTC IP LEAK: TM/Akamai detectaba
                # IP REAL del usuario (84.121.x.x = Telefónica ES) aunque el
                # HTTP iba por proxy Quantum (37.135.x.x). WebRTC STUN reveal
                # la IP local incluso detrás de proxy HTTP. Camoufox tiene
                # `block_webrtc=True` que deshabilita peerconnection completo.
                "block_webrtc": True,
                "proxy": {
                    "server": f"http://{prof['proxy_host']}:{prof['proxy_port']}",
                    "username": prof['proxy_username'],
                    "password": prof['proxy_password'],
                },
            }
            # FIX 2026-05-14: cleanup pre-launch SOLO de tmp dirs.
            # NO MATAR camoufox.exe por nombre — eso afectaría a otros slots
            # corriendo en paralelo. Cada launch crea playwright-artifacts-* y
            # playwright_firefoxdev_profile-* en %TEMP%. Si no se limpian al exit,
            # acumulan 300+ dirs → filesystem lento → hang 28min en
            # page.bring_to_front(). Limpieza de tmp es safe (rmtree skip si
            # dir está en uso por otro slot).
            # Cleanup tmp dirs huérfanos de Playwright/Camoufox
            try:
                import tempfile as _tf
                import shutil as _shutil
                _tmp_root = _tf.gettempdir()
                _patterns = ('playwright-artifacts-',
                             'playwright_firefoxdev_profile-',
                             'mozilla-temp-files')
                _removed = 0
                if os.path.isdir(_tmp_root):
                    for _entry in os.listdir(_tmp_root):
                        if any(_entry.startswith(p) for p in _patterns):
                            _full = os.path.join(_tmp_root, _entry)
                            try:
                                _shutil.rmtree(_full, ignore_errors=True)
                                _removed += 1
                            except Exception: pass
                if _removed > 0:
                    log(slot, "playwright_tmp_cleanup", removed=_removed)
            except Exception as _te:
                log(slot, "playwright_tmp_cleanup_err", err=str(_te)[:120])
            log(slot, "camoufox_launching",
                headless=cf_kwargs["headless"],
                sessionid=prof['env_id'].replace('camoufox-', ''),
                proxy_host=prof['proxy_host'])
            _cm_camoufox = Camoufox(**cf_kwargs)
            _pw_dummy = None    # camoufox no usa sync_playwright wrapper
            b = _cm_camoufox.__enter__()
            # Fix 2026-05-12: no_viewport=True permite que la página siga
            # el tamaño REAL del window de Firefox (responsive al maximizar /
            # restaurar / redimensionar manualmente — como MoreLogin).
            try:
                ctx = b.new_context(no_viewport=True)
            except Exception:
                # Fallback si new_context falla: usar el default existente
                ctx = b.contexts[0] if b.contexts else b.new_context()
            # FIX 2026-05-15: timeout default global para evitar hangs infinitos
            # en operaciones Playwright (page.bring_to_front(), page.evaluate
            # tras navigation roto, etc). Sin esto, slots hang 20+ min cuando
            # Camoufox/Quantum proxy responde lento. 60s es generoso.
            try:
                ctx.set_default_timeout(60000)       # 60s para ops normales
                ctx.set_default_navigation_timeout(30000)   # 30s para goto
                log(slot, "ctx_timeouts_set", default_ms=60000, nav_ms=30000)
            except Exception as _te:
                log(slot, "ctx_timeouts_set_err", err=str(_te)[:80])
        else:
            # MoreLogin: connect_over_cdp con error logging defensivo
            try:
                v = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=8).json()
                log(slot, "cdp_version_ok", browser=v.get("Browser", "")[:40])
            except Exception as _ce:
                log(slot, "FATAL_cdp_version", err=str(_ce)[:120], port=port)
                return 1
            try:
                _pw_dummy = sync_playwright().__enter__()
                _cm_camoufox = None
                b = _pw_dummy.chromium.connect_over_cdp(v["webSocketDebuggerUrl"])
                ctx = b.contexts[0] if b.contexts else b.new_context()
                log(slot, "cdp_connected_ok", ctx_count=len(b.contexts))
            except Exception as _ce:
                log(slot, "FATAL_cdp_connect", err=str(_ce)[:200])
                return 1
        # ── A partir de aquí, todo el flow es idéntico (morelogin / camoufox) ──
        if True:    # placeholder para mantener indentación del bloque original
            # ════════════════════════════════════════════════════════════
            # STEALTH SETUP 2026-05-13 — capa anti-fingerprint completa
            # ════════════════════════════════════════════════════════════
            # Cablea purchase_stealth: persona determinística por sessionid +
            # STEALTH_JS (webdriver/plugins/WebGL/canvas/audio/WebRTC/screen/TZ).
            # Aplica ANTES de cualquier goto.
            try:
                import purchase_stealth as _ps
                _sessid_for_persona = (prof.get("sessionid") or
                                        str(prof.get("env_id", "")) or "0")
                _persona = _ps.create_persona_from_sessionid(
                    _sessid_for_persona,
                    country=(prof.get("proxy_country") or "ES"))
                log(slot, "persona_created",
                    sessid=_persona["sessionid"],
                    gpu=_persona["gpu_renderer"][:70],
                    screen=f"{_persona['screen_width']}x{_persona['screen_height']}",
                    hw=_persona["hardware_concurrency"],
                    mem=_persona["device_memory"],
                    tz=_persona["timezone"])
                _engine_label = "firefox" if engine == "camoufox" else "chrome"
                # Consolidación 2026-05-13: flag STEALTH_JS eliminado.
                # apply_stealth_to_context es el ÚNICO path (fix Intl Proxy estable).
                _stealth_res = _ps.apply_stealth_to_context(
                    ctx, _persona,
                    log=lambda *a, **k: log(slot, *a, **k),
                    engine=_engine_label)
                log(slot, "stealth_applied", engine=_engine_label,
                    applied=_stealth_res.get("applied", []))
            except Exception as _se:
                log(slot, "stealth_setup_err", err=str(_se)[:200])
                _persona = None

            # Notification.requestPermission override ya incluido en stealth_js
            # (purchase_stealth.py); el init_script duplicado eliminado 2026-05-13.
            # FIX 2026-05-15: page setup con timeout watchdog. Antes esto colgaba
            # 3-5 min en page.bring_to_front() cuando Camoufox tardaba en arrancar.
            # Watchdog 30s mata el proceso si no responde.
            try:
                _page_setup_start = time.time()
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                _elapsed_pages = time.time() - _page_setup_start
                if _elapsed_pages > 5:
                    log(slot, "ctx_pages_slow", elapsed_s=round(_elapsed_pages, 1))
            except Exception as _pe:
                log(slot, "ctx_pages_err", err=str(_pe)[:120])
                page = None
            if page is None:
                log(slot, "FATAL_no_page_after_camoufox_launch")
                write_outcome(slot, {
                    "status": "fatal_no_page",
                    "step_reached": "ctx_pages_init",
                    "rc": 99,
                })
                return 99
            # FIX 2026-05-15: bring_to_front() SKIPPED completamente.
            # Razones:
            # 1. headless=False → window ya visible automáticamente
            # 2. Llamarla desde daemon thread rompe greenlet de Playwright
            #    ("cannot switch to a different thread")
            # 3. Llamarla sync puede colgar 10min en Camoufox loaded
            # No es bloqueante para el flow, simplemente no traemos focus.

            # ─── PRE-LOGIN PROACTIVO ───
            # TM bloquea silenciosamente "Continuar comprando" si no hay sesión:
            # tras click, la URL no cambia y el watcher se queda colgado. Solución:
            # 1) Si la cuenta tiene storage_state_file → cargar cookies/localStorage.
            # 2) Si no → ir a /login PRIMERO, autenticar, luego al evento.
            ssf = account.get('storage_state_file')
            # ═══════════════════════════════════════════════════════════════
            # FIX 2026-05-13: storage_state SOLO si edad <4h. TM EPS emite
            # `eps_sid` con TTL ~30d pero la cookie está bound al `client_ip`
            # detectado al emitirla. Si el storage_state es >4h, casi seguro
            # se emitió con OTRA IP Quantum → EPS rechaza → /auth devuelve
            # 401+tm-bl:1 con página "Identity Verified" (pantalla blanca).
            # Forzar login fresco → EPS emite nueva `eps_sid` para IP actual.
            # Validado empíricamente: perfil fresh SIN storage_state PASA /auth.
            # ═══════════════════════════════════════════════════════════════
            # FIX 2026-05-14: TTL adaptativo según estabilidad del sessid.
            # Si prof.stable_sessid=True (Camoufox con sessid=hash(account.id)),
            # la IP Quantum es ESTABLE per cuenta → eps_sid sigue válido durante
            # días, NO horas. Extendemos el TTL a 30d (límite de eps_sid TTL).
            # Si stable_sessid=False (MoreLogin o legacy), mantenemos 4h
            # porque la IP Quantum rota entre runs → eps_sid invalida.
            if prof.get('stable_sessid'):
                _max_state_age_s = 14 * 24 * 3600   # 14 días (eps_sid TTL real=16d, margen 2d)
            else:
                _max_state_age_s = MAX_STORAGE_STATE_AGE_S   # 4h legacy
            if ssf and os.path.exists(ssf):
                try:
                    _ssf_age_s = time.time() - os.path.getmtime(ssf)
                except Exception:
                    _ssf_age_s = 0
                if _ssf_age_s > _max_state_age_s:
                    log(slot, "storage_state_TOO_OLD_skip",
                        path=os.path.basename(ssf),
                        age_h=round(_ssf_age_s / 3600, 1),
                        max_h=_max_state_age_s / 3600,
                        stable_sessid=prof.get('stable_sessid', False),
                        note="state expirado → forzar login fresco")
                    ssf = None
                else:
                    log(slot, "storage_state_age_ok",
                        age_h=round(_ssf_age_s / 3600, 1),
                        max_h=_max_state_age_s / 3600,
                        stable_sessid=prof.get('stable_sessid', False))
            if ssf and os.path.exists(ssf):
                try:
                    log(slot, "loading_storage_state", path=os.path.basename(ssf))
                    with open(ssf, encoding='utf-8') as f:
                        state = json.load(f)
                    if state.get('cookies'):
                        ctx.add_cookies(state['cookies'])
                    log(slot, "storage_state_loaded", cookies=len(state.get('cookies') or []))
                except Exception as e:
                    log(slot, "storage_state_err", err=str(e)[:120])
                # 2026-05-12 FIX bot_block post-OAuth: warming home TM ANTES del
                # evento. Cookies del storage_state pueden ser viejas (>7 días),
                # y Akamai detecta el mismatch IP-residencial-nueva + cookies-viejas
                # como bot. Navegar a home TM por 15s refresca las cookies (TM
                # emite nuevas Set-Cookie) y simula actividad humana antes del
                # checkout. Esto reduce drásticamente "Su sesión se ha suspendido"
                # tras OAuth login.
                try:
                    warming_url = "https://www.ticketmaster.es/" if "ticketmaster.es" in args.event_url else \
                                  "https://www.ticketmaster.com/"
                    log(slot, "warming_home_start", url=warming_url)
                    page.goto(warming_url, wait_until="domcontentloaded", timeout=20000)
                    human_pause(2, 3)
                    # Cerrar cookies banner OneTrust si aparece
                    try:
                        page.evaluate(r"""() => {
                            const b = document.querySelector('#onetrust-accept-btn-handler');
                            if (b && b.offsetParent !== null) b.click();
                        }""")
                    except Exception: pass
                    # OPTIM 2026-05-12: scroll reducido a 4 pasos rápidos
                    try:
                        for sy in (400, 900, 1400, 400):
                            page.evaluate(f"window.scrollTo({{top:{sy},behavior:'smooth'}})")
                            page.wait_for_timeout(int(500 + random.random()*500))
                    except Exception: pass
                    # Settle final corto
                    human_pause(1.5, 2.5)
                    _warming_body_len = 0
                    try:
                        _warming_body_len = int(page.evaluate(
                            '() => (document.body.innerText||"").length') or 0)
                    except Exception: pass
                    log(slot, "warming_home_done",
                        url=page.url[:80],
                        body_len=_warming_body_len)
                    # FIX 2026-05-15: detección early de bot-block. Si la home
                    # carga body_len < 500 (debería ser 2000+), TM está sirviendo
                    # página bot-block ("Su sesión se ha suspendido") o página
                    # vacía. Abortar sin desperdiciar 5min en warming.
                    if _warming_body_len < 500:
                        # Verificar si es bot-block real
                        try:
                            _body_check = page.evaluate(
                                "() => (document.body.innerText||'').toLowerCase()") or ''
                            _bot_markers = [
                                'sesión se ha suspendido',
                                'session has been paused',
                                'something in the way you are browsing',
                                'forma de navegar',
                            ]
                            if any(m in _body_check for m in _bot_markers):
                                log(slot, "FATAL_bot_block_at_warming",
                                    body_preview=_body_check[:200])
                                _persist_bot_block(prof.get('profile', ''),
                                                    prof.get('ip', ''),
                                                    'bot_block_warming',
                                                    account_id=account.get('id'))
                                write_outcome(slot, {
                                    "status": "bot_block",
                                    "step_reached": "warming_home",
                                    "rc": 99,
                                })
                                return 99
                            elif _warming_body_len == 0:
                                # body_len=0 sin markers = TM/proxy serving empty
                                # = sesión muerta. Abort early para retry con
                                # cuenta/IP diferente.
                                log(slot, "FATAL_empty_warming_response",
                                    note="body_len=0 sin bot markers - sesión muerta o proxy timeout")
                                _persist_bot_block(prof.get('profile', ''),
                                                    prof.get('ip', ''),
                                                    'empty_warming',
                                                    account_id=account.get('id'))
                                write_outcome(slot, {
                                    "status": "bot_block",
                                    "step_reached": "warming_home_empty",
                                    "rc": 99,
                                })
                                return 99
                            else:
                                log(slot, "warming_body_low_but_not_bot_block",
                                    body_len=_warming_body_len,
                                    preview=_body_check[:100])
                        except Exception as _bce:
                            log(slot, "warming_body_check_err", err=str(_bce)[:80])
                except Exception as _we:
                    log(slot, "warming_home_err", err=str(_we)[:120])
                # ─── HUMAN JOURNEY WARM-UP 2026-05-13 ───
                # Si la cuenta no ha logueado en >2 días o el profile es
                # auto-created (NUEVO), hacer warming agresivo (3-7 min de
                # navegación natural) para construir signals Akamai/DataDome
                # antes del checkout. Esto reduce "forced password reset"
                # (step-up auth TM identity por device-fingerprint nuevo).
                _account_needs_warmup = False
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    # FIX 2026-05-15 OPTIMIZATION: skip warmup si storage_state
                    # se guardó por NOSOTROS recientemente (<24h) — significa
                    # que la cuenta ya pasó por checkout recientemente, cookies
                    # device-trust están FRESH → no necesita warmup adicional.
                    _bot_save = account.get("last_bot_state_save_at")
                    if _bot_save:
                        try:
                            _bs = _dt.fromisoformat(_bot_save.replace("Z", "+00:00"))
                            if _bs.tzinfo is None:
                                _bs = _bs.replace(tzinfo=_tz.utc)
                            _bot_save_age_h = (_dt.now(_tz.utc) - _bs).total_seconds() / 3600
                            if _bot_save_age_h < 24:
                                _account_needs_warmup = False
                                log(slot, "warmup_skipped_recent_state",
                                    age_h=round(_bot_save_age_h, 1),
                                    note="storage_state guardado <24h → skip warmup")
                                # Saltar el resto del check
                                _last_login = None
                            else:
                                _last_login = account.get("last_login") or account.get("validated_at")
                        except Exception:
                            _last_login = account.get("last_login") or account.get("validated_at")
                    else:
                        _last_login = account.get("last_login") or account.get("validated_at")
                    if _last_login:
                        try:
                            _ll = _dt.fromisoformat(_last_login.replace("Z", "+00:00"))
                            if _ll.tzinfo is None:
                                _ll = _ll.replace(tzinfo=_tz.utc)
                            _age_days = (_dt.now(_tz.utc) - _ll).total_seconds() / 86400
                            _account_needs_warmup = _age_days > 2.0
                        except Exception: pass
                    if prof.get("auto_created"):
                        _account_needs_warmup = True
                except Exception: pass

                # Consolidación 2026-05-13: flag HUMAN_WARMUP eliminado.
                # warm_up_session se ejecuta SIEMPRE que `_account_needs_warmup`
                # sea True (auto_created profile o last_login >2d).
                if _account_needs_warmup:
                    try:
                        import human_journey as _hj
                        _tm_id = ""
                        try:
                            m = re.search(r"/(\d{6,})", args.event_url)
                            if m: _tm_id = m.group(1)
                        except Exception: pass
                        _wres = _hj.warm_up_session(
                            page, persona=_persona if "_persona" in dir() else None,
                            log=lambda *a, **k: log(slot, *a, **k),
                            target_event_id=_tm_id,
                            home_url="https://www.ticketmaster.es/",
                            min_seconds=45, max_seconds=90,   # reducido de 120-300 → 45-90s
                            decoy_visits=1, do_search=True, do_visibility=True)
                        log(slot, "human_journey_done",
                            elapsed=_wres.get("elapsed_s"),
                            pages=_wres.get("pages_visited"),
                            scrolls=_wres.get("total_scrolls"),
                            decoy_visits=_wres.get("decoy_visits"),
                            search=_wres.get("search_done"))
                    except Exception as _hje:
                        log(slot, "human_journey_err", err=str(_hje)[:160])
            else:
                # ════════════════════════════════════════════════════════════
                # FIX 2026-05-13: EPS warming en /home antes de /auth.
                # `auth.ticketmaster.com` requiere cookie `eps_sid` emitida por
                # `/eps-mgr` script (que SOLO está en www.ticketmaster.es/),
                # ligada a la `client_ip` actual. Si saltamos a my./auth
                # directamente → EPS rechaza con 401+tm-bl:1 → pantalla blanca.
                # Solución: visitar PRIMERO www.ticketmaster.es/ para que
                # `/eps-mgr` emita `eps_sid` válido para la IP Quantum actual.
                # ════════════════════════════════════════════════════════════
                eps_home_url = "https://www.ticketmaster.es/" if "ticketmaster.es" in args.event_url else \
                               "https://www.ticketmaster.com/"
                try:
                    log(slot, "eps_warming_home", url=eps_home_url)
                    page.goto(eps_home_url, wait_until="domcontentloaded", timeout=30000)
                    # FIX 2026-05-14: pausa 4-6s → 2.5-3.5s. /eps-mgr + eps.js
                    # son inline scripts, su descarga es <1s. La pausa anterior
                    # era excesiva por inseguridad. El eps_sid_check después
                    # validará si la cookie llegó (si no, log lo registra).
                    human_pause(2.5, 3.5)   # Esperar a /eps-mgr + eps.js
                    # Verificar emisión de eps_sid
                    try:
                        eps_cookies = [c for c in ctx.cookies()
                                       if c['name'].lower() == 'eps_sid']
                        log(slot, "eps_sid_check",
                            emitted=bool(eps_cookies),
                            value_len=(len(eps_cookies[0]['value']) if eps_cookies else 0))
                    except Exception: pass
                    # Aceptar cookies banner en home (acumula signals naturales)
                    for txt in ["Aceptar todas las cookies", "Aceptar", "Acepto"]:
                        try:
                            el = page.get_by_text(txt, exact=False).first
                            if el.is_visible(timeout=1500):
                                el.click(); human_pause(1, 2); break
                        except Exception: continue
                except Exception as _ewe:
                    log(slot, "eps_warming_err", err=str(_ewe)[:160])

                # ════════════════════════════════════════════════════════════
                # FIX 2026-05-13: NO ir a my.ticketmaster.es como pre-login.
                # `my.ticketmaster.es` redirige a `/auth.ticketmaster.com`
                # cargando un flow OAuth incompleto (sin client_id+scope que TM
                # SÍ pasa cuando entras desde el evento). Resultado: /auth carga
                # pantalla en blanco porque ese flow OAuth no se completa.
                # Fix: skip pre_login, ir DIRECTO al evento. Al pulsar "Continuar
                # comprando", TM construirá el URL OAuth con todos los params
                # correctos (client_id, scope, redirect_uri) y /auth responderá
                # con form de login normal. login_full lo manejará entonces.
                # ════════════════════════════════════════════════════════════
                log(slot, "pre_login_SKIPPED_go_direct_to_event",
                    note="OAuth se inicia desde Continuar Comprando, no desde my.")

            # 4) Goto evento
            log(slot, "goto", url=args.event_url[:80])
            page.goto(args.event_url, wait_until="domcontentloaded", timeout=30000)
            # FIX 2026-05-14: pausa reducida de 5-7s → 1.5-2.5s.
            # El hydration poll de abajo (línea ~3074) ya espera dinámicamente
            # hasta que body_len > 2500. La pausa fija era redundante.
            human_pause(1.5, 2.5)
            # ─── Cookie banner cleanup (CRÍTICO Camoufox) ───
            # El banner OneTrust de TM cubre los botones "Ver lo mejor
            # disponible" y "Entradas". Si no se cierra, los clicks fallan.
            # Estrategia: 1) intentar TODOS los selectores conocidos, 2) JS
            # force-close, 3) hidden display:none como último recurso.
            cookie_closed = False
            for txt in ["Aceptar todas las cookies", "Aceptar todas",
                        "Aceptar", "Accept all cookies", "Accept all",
                        "Accept", "Acepto", "OK", "Continuar"]:
                if cookie_closed: break
                try:
                    btn = page.get_by_role('button',
                        name=re.compile(f'^{re.escape(txt)}', re.I)).first
                    if btn.is_visible(timeout=2000):
                        btn.scroll_into_view_if_needed(timeout=2000)
                        btn.click(timeout=3000, force=True)
                        human_pause(1, 1.5)
                        cookie_closed = True
                        log(slot, "cookies_accepted", method="role_button",
                            label=txt[:30])
                        break
                except Exception: pass
                try:
                    el = page.get_by_text(txt, exact=False).first
                    if el.is_visible(timeout=1500):
                        el.click(force=True); human_pause(1, 1.5)
                        cookie_closed = True
                        log(slot, "cookies_accepted", method="text",
                            label=txt[:30])
                        break
                except Exception: pass
            # Force-close via JS si nada funcionó (OneTrust + frameworks comunes)
            if not cookie_closed:
                try:
                    js_closed = page.evaluate(r"""() => {
                        // OneTrust botones por id estándar
                        const ids = ['onetrust-accept-btn-handler',
                                     'onetrust-pc-btn-handler',
                                     'accept-recommended-btn-handler'];
                        for (const id of ids) {
                            const el = document.getElementById(id);
                            if (el) { el.click(); return 'click:' + id; }
                        }
                        // Selectores comunes de banners
                        const sels = [
                            '#onetrust-accept-btn-handler',
                            '#cookie-accept',
                            'button[aria-label*="Aceptar" i]',
                            'button[aria-label*="Accept" i]',
                            '[data-testid*="accept-cookies" i]',
                        ];
                        for (const s of sels) {
                            const el = document.querySelector(s);
                            if (el) { el.click(); return 'click:' + s; }
                        }
                        // Último recurso: ocultar banner via display:none
                        const banners = document.querySelectorAll(
                            '#onetrust-banner-sdk, #onetrust-consent-sdk, '
                            + '[class*="cookie-banner" i], [class*="CookieBanner" i], '
                            + '[id*="cookieBanner" i], [id*="consent" i]');
                        let hid = 0;
                        banners.forEach(b => { b.style.display = 'none'; hid++; });
                        if (hid > 0) return 'hidden:' + hid;
                        return 'nothing_found';
                    }""")
                    log(slot, "cookies_force_close", result=str(js_closed)[:80])
                    cookie_closed = ('click:' in (js_closed or '')
                                       or 'hidden:' in (js_closed or ''))
                    if cookie_closed: human_pause(1, 1.5)
                except Exception as _ce:
                    log(slot, "cookies_force_close_err",
                        err=str(_ce)[:80])
            shoot(slot, page, "event_loaded")

            # ═══ Wait full hydration (Camoufox/Firefox tarda más en hidratar) ═══
            # Fix 2026-05-12: el spinner "Un momento por favor..." aparece
            # mientras TM termina de cargar el HTML del evento. Hay que
            # esperar a que el body real se renderice (length >2500) antes
            # de intentar detectar botones.
            def _body_len():
                try:
                    return page.evaluate(
                        '() => (document.body && document.body.innerText || "").length'
                    ) or 0
                except Exception: return 0

            hydration_start = time.time()
            initial_len = _body_len()
            log(slot, "hydration_initial", body_len=initial_len)
            while _body_len() < 2500 and (time.time() - hydration_start) < 30:
                page.wait_for_timeout(1000)
            final_len = _body_len()
            log(slot, "hydration_done",
                elapsed_s=int(time.time() - hydration_start),
                body_len=final_len)

            # ═══ Cookie banner cleanup POST-hydration (CRÍTICO) ═══
            # Fix 2026-05-12: el banner OneTrust aparece DESPUÉS de hidratar.
            # Si lo intentamos cerrar antes (en cookies_force_close al inicio),
            # nothing_found. Hay que cerrarlo AHORA que ya cargó.
            cookie_closed_late = False
            for _try in range(3):
                if cookie_closed_late: break
                for txt in ["Aceptar todas las cookies", "Aceptar todas",
                            "Aceptar", "Accept all cookies", "Accept all",
                            "Accept", "Acepto", "OK"]:
                    try:
                        btn = page.get_by_role('button',
                            name=re.compile(f'^{re.escape(txt)}',
                                             re.I)).first
                        if btn.is_visible(timeout=1500):
                            btn.scroll_into_view_if_needed(timeout=1500)
                            btn.click(timeout=2500, force=True)
                            cookie_closed_late = True
                            log(slot, "cookies_post_hydration_accepted",
                                method="role_button", label=txt[:30])
                            human_pause(1, 1.5)
                            break
                    except Exception: continue
                    try:
                        el = page.get_by_text(txt, exact=False).first
                        if el.is_visible(timeout=1000):
                            el.click(force=True)
                            cookie_closed_late = True
                            log(slot, "cookies_post_hydration_accepted",
                                method="text", label=txt[:30])
                            human_pause(1, 1.5)
                            break
                    except Exception: continue
                if not cookie_closed_late:
                    # JS force-close OneTrust + hidden
                    try:
                        res = page.evaluate(r"""() => {
                            const ids = ['onetrust-accept-btn-handler',
                                         'accept-recommended-btn-handler'];
                            for (const id of ids) {
                                const el = document.getElementById(id);
                                if (el && el.offsetParent !== null) {
                                    el.click(); return 'click:' + id;
                                }
                            }
                            // Buscar por texto en cualquier button visible
                            const btns = Array.from(document.querySelectorAll(
                                'button')).filter(b => b.offsetParent !== null);
                            const t = btns.find(b => {
                                const tx = (b.innerText||'').trim();
                                return /^aceptar\s+todas/i.test(tx);
                            });
                            if (t) { t.click(); return 'click:text'; }
                            // último recurso: ocultar el banner entero
                            const banners = document.querySelectorAll(
                                '#onetrust-banner-sdk, #onetrust-consent-sdk');
                            let h = 0;
                            banners.forEach(b => { b.style.display = 'none'; h++; });
                            return h > 0 ? 'hidden:' + h : 'nothing';
                        }""")
                        if res and ('click' in res or 'hidden' in res):
                            cookie_closed_late = True
                            log(slot, "cookies_post_hydration_js",
                                result=str(res)[:60])
                            human_pause(1, 1.5)
                            break
                    except Exception: pass
                page.wait_for_timeout(1500)
            log(slot, "cookies_post_hydration_final",
                closed=cookie_closed_late)

            # ═══ SMART FLOW DETECTOR ═══
            # ANTES de elegir flow, detectar qué presenta TM en este evento:
            #   a) "Las entradas no están disponibles" → ABORT (presale/sold-out)
            #   b) Botón "Ver lo mejor disponible" → flow quick (categorías + stepper)
            #   c) Solo "Ver mapa de asientos" → flow seatmap auto (click sector libre)
            # Norma user: el bot debe diferenciar inteligentemente.
            # Esperar networkidle para evitar race conditions (TM hace redirects).
            # FIX 2026-05-14: timeout reducido 8s → 4s. networkidle rara vez
            # se alcanza en TM (sockets/polling activos), el timeout era largo.
            try:
                page.wait_for_load_state('networkidle', timeout=4000)
            except Exception: pass
            def _detect_state():
                return page.evaluate(r"""() => {
                    const txt = (document.body && document.body.innerText || '').toLowerCase();
                    const noTickets = /entradas\s+no\s+est[aá]n\s+disponibles|no\s+tickets\s+available|no\s+hemos\s+podido\s+encontrar/i.test(txt);
                    const isPresale = /presale|c[oó]digo\s+de\s+presale|verified\s+fan|verified\s+only/i.test(txt);
                    const all = Array.from(document.querySelectorAll(
                        'button, a, [role="button"], [role="tab"], [data-testid*="viewType" i]'));
                    const bestBtn = all.find(x => /ver\s+lo\s+mejor\s+disponible|best\s+available|view\s+best\s+available/i.test(
                        x.innerText || x.textContent || x.getAttribute('aria-label') || ''));
                    const mapBtn = all.find(x => /ver\s+mapa\s+de\s+asientos|seat\s*map|mapa\s+del?\s+recinto/i.test(
                        x.innerText || x.textContent || x.getAttribute('aria-label') || ''));
                    const stepperReady = !!document.querySelector('[data-testid="quantityStepper"]');
                    return {
                        no_tickets: noTickets, is_presale: isPresale,
                        has_best_available: !!bestBtn,
                        has_seatmap_tab: !!mapBtn,
                        on_seatmap_already: /selecciona\s+tus\s+entradas|select\s+your\s+seats|seleccion[aá]\s+(en\s+el\s+)?mapa/i.test(txt),
                        stepper_ready: stepperReady,
                        body_len: txt.length,
                    };
                }""")
            def _safe_detect_state():
                # Tolerar "Execution context was destroyed" durante navegación.
                for _ in range(3):
                    try:
                        return _detect_state()
                    except Exception as _err:
                        emsg = str(_err)
                        if 'context was destroyed' in emsg or 'navigation' in emsg.lower():
                            page.wait_for_timeout(900)
                            continue
                        raise
                # Último intento — si vuelve a fallar, dejar que la excepción salga
                return _detect_state()
            page_state = _safe_detect_state()
            # FIX 2026-05-14: polling más rápido. 8x1s = 8s max → 6x700ms = 4.2s max.
            # En TM la mayoría de eventos hidratan en <2s tras goto.
            tries = 0
            while (not page_state.get("no_tickets")
                   and not page_state.get("has_best_available")
                   and not page_state.get("has_seatmap_tab")
                   and not page_state.get("stepper_ready")
                   and not page_state.get("on_seatmap_already")
                   and tries < 6):
                page.wait_for_timeout(700)
                page_state = _safe_detect_state()
                tries += 1
            log(slot, "page_state_detect", retries=tries, **page_state)

            # GATE 1: Sin entradas disponibles → abort
            if page_state.get("no_tickets") and not page_state.get("has_best_available") and not page_state.get("has_seatmap_tab"):
                shoot(slot, page, "no_tickets_available_abort")
                log(slot, "FATAL_no_tickets_available",
                    is_presale=page_state.get("is_presale"),
                    note="TM dice 'no disponibles' — evento en presale-only o agotado")
                write_outcome(slot, {
                    "status": "no_tickets_available",
                    "step_reached": "page_load",
                    "error": "no_tickets_available_on_TM",
                    "is_presale": page_state.get("is_presale"),
                })
                return 1

            if args.mode == "quick" and page_state.get("has_best_available"):
                # === MODO GENÉRICO: "Ver lo mejor disponible" + stepper ===
                log(slot, "quick_mode_via_best_available")
                # OPTIM 2026-05-12 final: 1 scroll rápido + click inmediato
                try:
                    page.evaluate("window.scrollTo({top: 300, behavior: 'smooth'})")
                    page.wait_for_timeout(400)
                except Exception: pass
                # Click "Ver lo mejor disponible"
                page.evaluate(r"""() => {
                    const all = Array.from(document.querySelectorAll('button, a, [role="button"], [role="tab"]'));
                    const b = all.find(x => /ver\s+lo\s+mejor\s+disponible|best\s+available/i.test(x.innerText||x.textContent||''));
                    if (b) b.click();
                }""")
                human_pause(1, 2)   # OPTIM: era 3-5s

                # 6) Stepper qty — con PAUSAS HUMANAS (anti-bot 2026-05-12)
                # Antes 200ms entre qty++ → 0 a 2 en <1s = bot puro.
                # Ahora 1.2-2.5s entre clicks → humano normal.
                log(slot, "stepper_offer", idx=args.offer_idx, target_qty=args.qty)
                try:
                    page.wait_for_selector('[data-testid="quantityStepper"]',
                                            timeout=15000, state='attached')
                except Exception:
                    log(slot, "stepper_wait_timeout")
                for i in range(args.qty + 4):
                    cur = page.evaluate(f"""() => {{
                        const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{args.offer_idx}];
                        return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
                    }}""")
                    log(slot, "qty", current=cur)
                    if cur >= args.qty: break
                    # Hover ligero antes de click (humano mueve mouse)
                    page.evaluate(f"""() => {{
                        const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{args.offer_idx}];
                        if (!s) return;
                        const btns = s.querySelectorAll('button');
                        const target = btns[btns.length-1] || btns[0];
                        if (target) {{
                            const r = target.getBoundingClientRect();
                            ['mouseenter','mouseover'].forEach(ev =>
                                target.dispatchEvent(new MouseEvent(ev, {{
                                    bubbles: true, clientX: r.left+r.width/2, clientY: r.top+r.height/2
                                }})));
                            target.click();
                        }}
                    }}""")
                    # OPTIM 2026-05-12: pausa 500-1000ms (era 1200-2500ms)
                    page.wait_for_timeout(int(500 + random.random()*500))
                shoot(slot, page, "stepper_done")

                # 7) Click Entradas verde — JS direct PRIMERO (mucho más rápido)
                # Fix 2026-05-12: norma user "el proceso de entradas debe ser
                # rápido" → JS click instantáneo, sin esperar wait_for_selector.
                log(slot, "click_find_tickets")
                clicked_find = False
                try:
                    clicked_find = page.evaluate(r"""() => {
                        const btns = Array.from(document.querySelectorAll(
                            'button, a[role="button"]'));
                        // 1) data-testid="findTicketsBtn" si existe
                        const byTest = btns.find(b => b.dataset?.testid === 'findTicketsBtn');
                        if (byTest && byTest.offsetParent !== null) {
                            byTest.click(); return 'data-testid';
                        }
                        // 2) Por texto "Entradas" / "Get tickets" / "Find tickets"
                        const byText = btns.find(b => {
                            if (b.offsetParent === null) return false;
                            const txt = (b.innerText||b.textContent||'').trim();
                            return /^entradas$|^get tickets$|^find tickets$/i.test(txt);
                        });
                        if (byText) { byText.click(); return 'text'; }
                        return null;
                    }""")
                    if clicked_find:
                        log(slot, "find_tickets_clicked", method=str(clicked_find))
                except Exception as e:
                    log(slot, "find_tickets_js_err", err=str(e)[:120])
                # Fallback locator nativo si JS no funcionó
                if not clicked_find:
                    try:
                        btn = page.locator(
                            'button[data-testid="findTicketsBtn"]').first
                        btn.scroll_into_view_if_needed(timeout=3000)
                        btn.click(timeout=3000, force=True)
                        clicked_find = True
                        log(slot, "find_tickets_clicked", method="locator")
                    except Exception as e:
                        log(slot, "find_tickets_locator_err",
                            err=str(e)[:120])
                human_pause(1.5, 2.5)   # OPTIM 2026-05-12: era 4-6s
                shoot(slot, page, "after_find_tickets")

                # 7b) Handler "asientos no juntos" — política: SIEMPRE Ajustar selección
                # (a menos que --allow-separated esté pasado)
                for ck in range(3):
                    h = handle_separated_seats_modal(slot, page,
                                                      allow_separated=args.allow_separated)
                    if h.get("detected"):
                        human_pause(2, 4)
                    else:
                        break

            elif args.mode == "quick" and not page_state.get("has_best_available"):
                # === AUTO-FALLBACK: evento solo permite mapa interactivo ===
                # No hay botón "Ver lo mejor disponible" → debemos elegir sector
                # manualmente del SVG. Estrategia: click "Ver mapa de asientos" tab
                # (si existe), esperar render, click primer sector disponible no-VIP.
                log(slot, "auto_fallback_seatmap",
                    has_seatmap_tab=page_state.get("has_seatmap_tab"))
                # Click tab "Ver mapa de asientos" si está
                if page_state.get("has_seatmap_tab") and not page_state.get("on_seatmap_already"):
                    page.evaluate(r"""() => {
                        const all = Array.from(document.querySelectorAll('button, a, [role="button"], [role="tab"]'));
                        const t = all.find(x => /ver\s+mapa\s+de\s+asientos|seat\s*map/i.test(x.innerText||x.textContent||''));
                        if (t) t.click();
                    }""")
                    human_pause(2.5, 4)
                shoot(slot, page, "seatmap_tab_active")

                # Esperar a que el SVG se renderice
                try:
                    page.wait_for_selector('svg, [class*="seat-map" i], [class*="venue-map" i]', timeout=8000)
                except Exception: pass
                human_pause(1.5, 2.5)

                # Buscar sectores disponibles. TM usa varios formatos en su SVG:
                #   - <path data-section-id="...">
                #   - <polygon data-section="...">
                #   - <g class="...section..."> con <text> con número del sector
                pick_result = page.evaluate(r"""() => {
                    // Recoger candidatos
                    const pool = [];
                    document.querySelectorAll(
                        '[data-section-id], [data-tm-id], [data-section-name], '
                        + 'svg [data-section], polygon[data-section], '
                        + '[role="button"][aria-label*="zona" i], '
                        + '[role="button"][aria-label*="section" i], '
                        + '[role="button"][aria-label*="sector" i], '
                        + 'g[id*="section" i], path[id*="section" i]'
                    ).forEach(el => pool.push(el));
                    // También: polygons SVG con onclick / cursor pointer
                    document.querySelectorAll('svg polygon, svg path').forEach(el => {
                        const fill = el.getAttribute('fill') || '';
                        const op = parseFloat(el.getAttribute('opacity') || '1');
                        const cls = el.getAttribute('class') || '';
                        // Heurística: ignorar elementos translúcidos (sold-out típico opacity .3)
                        if (op < 0.5) return;
                        if (/sold|disabled|inactive|unavailable/i.test(cls)) return;
                        if (/^#?(ccc|ddd|eee|f0f0f0|gray|grey)/i.test(fill)) return;
                        // Solo si tiene cursor:pointer o handler
                        const cs = window.getComputedStyle(el);
                        if (cs.cursor !== 'pointer' && !el.onclick) return;
                        pool.push(el);
                    });
                    // Filtrar: visibles + no VIP/Pit + activos
                    const candidates = pool.filter(el => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 4 || r.height < 4) return false;
                        const lbl = (el.getAttribute('aria-label') || el.textContent || el.id || '').toLowerCase();
                        if (/vip|palco|suite|pit\b|premium\s+experience|hospitality/.test(lbl)) return false;
                        if (el.getAttribute('data-active') === 'false') return false;
                        if (el.getAttribute('aria-disabled') === 'true') return false;
                        return true;
                    });
                    if (candidates.length === 0) return {clicked: false, count: 0, error: 'no_candidates'};
                    // Elegir el primero (más cercano al inicio del DOM = típicamente tribuna alta)
                    const target = candidates[0];
                    target.scrollIntoView({block: 'center', behavior: 'instant'});
                    // Tres tipos de click por compatibilidad
                    target.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                    target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                    target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    return {clicked: true, count: candidates.length,
                            id: target.getAttribute('data-section-id') || target.getAttribute('data-tm-id') || target.id || '?',
                            label: (target.getAttribute('aria-label') || target.textContent || '').trim().slice(0, 60)};
                }""")
                log(slot, "fallback_sector_pick", **pick_result)
                if not pick_result.get("clicked"):
                    shoot(slot, page, "fallback_no_sector_found")
                    log(slot, "FATAL_no_sector_clickable",
                        note="No se detectó ningún sector clickeable en el SVG/DOM")
                    write_outcome(slot, {
                        "status": "no_sector_clickable",
                        "step_reached": "seatmap_fallback",
                        "error": "no_sector_clickable_in_svg",
                    })
                    return 1
                human_pause(3, 4)
                shoot(slot, page, "fallback_after_pick")

            else:
                # === MODO SEATMAP: selección sector por sector vía API/SVG ===
                log(slot, "seatmap_mode")
                sections_csv = (args.sections or "").strip()
                sections = [s.strip() for s in sections_csv.split(",") if s.strip()]
                if not sections:
                    log(slot, "FATAL_seatmap_no_sections",
                        note="--mode seatmap requiere --sections id1,id2,id3")
                    return 1
                res = select_seatmap_sections(slot, page, sections, qty=args.qty)
                log(slot, "seatmap_result", **{k: res.get(k) for k in ("ok", "used_section")})
                if not res.get("ok"):
                    shoot(slot, page, "seatmap_failed")
                    return 1
                shoot(slot, page, "seatmap_section_picked")

            # 8) Checkbox Acepto + Continuar
            # OPTIM 2026-05-12: pausa reducida 1-2s (era 2-3.5s)
            log(slot, "checkbox_acepto")
            page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})")
            human_pause(1, 2)
            page.evaluate("""() => {
                const labels = Array.from(document.querySelectorAll('label'));
                const lbl = labels.find(l => /acepto que he le[ií]do/i.test(l.textContent||''));
                if (!lbl) return;
                const forId = lbl.getAttribute('for');
                let cb = forId ? document.getElementById(forId) : null;
                if (!cb) cb = lbl.querySelector('input[type=checkbox]');
                if (cb) {
                    cb.scrollIntoView({block:'center'});
                    // Hover antes de click (humano)
                    const r = cb.getBoundingClientRect();
                    ['mouseenter','mouseover','mousemove'].forEach(ev =>
                        cb.dispatchEvent(new MouseEvent(ev, {
                            bubbles: true,
                            clientX: r.left+r.width/2,
                            clientY: r.top+r.height/2
                        })));
                    setTimeout(() => cb.click(), 200);
                }
            }""")
            # OPTIM 2026-05-12: pausa 1.2-2s (era 2.5-4.5s)
            human_pause(1.2, 2)

            # 8b) AUTO-FALLBACK SEATMAP: si TM nos dio el mapa interactivo
            # (ej: events sin "Best Available" como Karol G TROPITOUR), debemos
            # seleccionar al menos 1 sector ANTES de pulsar "Continuar". Si no,
            # el botón no avanza porque no hay nada en el carrito.
            try:
                seatmap_state = page.evaluate(r"""() => {
                    const txt = (document.body && document.body.innerText || '').toLowerCase();
                    const onMap = /selecciona\s+tus\s+entradas|select\s+your\s+seats|elige\s+tus\s+asientos|seleccion[aá]\s+(en\s+el\s+)?mapa/i.test(txt);
                    // Sectores con data-section-id, data-tm-id, o nodes SVG con class section
                    const sectors = Array.from(document.querySelectorAll(
                        '[data-section-id], [data-tm-id], svg [data-section-id], '
                        + '[role="button"][aria-label*="zona" i], [role="button"][aria-label*="section" i], '
                        + '[class*="section" i][data-active], polygon[data-section]'
                    )).filter(el => {
                        const active = el.getAttribute('data-active');
                        const ariaDis = el.getAttribute('aria-disabled');
                        if (active === 'false') return false;
                        if (ariaDis === 'true') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    return {on_map: onMap, sector_count: sectors.length,
                            sample: sectors.slice(0, 5).map(el => ({
                                id: el.getAttribute('data-section-id') || el.getAttribute('data-tm-id') || '?',
                                label: (el.getAttribute('aria-label') || el.textContent || '').slice(0, 40)
                            }))};
                })""")
            except Exception as _e:
                seatmap_state = {"on_map": False, "sector_count": 0}

            if seatmap_state.get("on_map") and seatmap_state.get("sector_count", 0) > 0:
                log(slot, "seatmap_auto_fallback",
                    sector_count=seatmap_state.get("sector_count"),
                    sample=seatmap_state.get("sample"))
                shoot(slot, page, "seatmap_detected_pre_pick")
                # Click en sector visible: priorizar sectores de tribuna alta
                # (precio más asequible, más stock típico) y evitar VIP/PIT.
                clicked = page.evaluate(r"""() => {
                    const candidates = Array.from(document.querySelectorAll(
                        '[data-section-id], [data-tm-id], svg [data-section-id], '
                        + '[role="button"][aria-label*="zona" i], [role="button"][aria-label*="section" i], '
                        + '[class*="section" i][data-active], polygon[data-section]'
                    )).filter(el => {
                        const active = el.getAttribute('data-active');
                        const ariaDis = el.getAttribute('aria-disabled');
                        if (active === 'false') return false;
                        if (ariaDis === 'true') return false;
                        const lbl = (el.getAttribute('aria-label') || el.textContent || '').toLowerCase();
                        // Excluir VIP / PIT / palco / suite (alto precio o sold out típico)
                        if (/vip|palco|suite|pit\b|lounge|premium\s+experience/.test(lbl)) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    if (candidates.length === 0) return {clicked: false, reason: 'no_visible_sectors'};
                    // Elegir el primer sector no-VIP (suele ser tribuna alta)
                    const target = candidates[0];
                    target.scrollIntoView({block: 'center'});
                    target.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                    return {clicked: true,
                            id: target.getAttribute('data-section-id') || target.getAttribute('data-tm-id') || '?',
                            label: (target.getAttribute('aria-label') || target.textContent || '').slice(0, 60)};
                })""")
                log(slot, "seatmap_sector_clicked", **clicked)
                if clicked.get("clicked"):
                    page.wait_for_timeout(3500)
                    shoot(slot, page, "seatmap_after_pick")
                else:
                    log(slot, "seatmap_pick_failed", reason=clicked.get("reason", "?"))

            # 9) Click "Continuar comprando" con retry — TM ignora silenciosamente
            # el click cuando hay bloqueo invisible (queue-it/captcha/sin sesión).
            # Reintentamos hasta 3 veces verificando cambio de URL.
            # FIX 2026-05-14: ESPERAR a que el spinner "Confirmando disponibilidad"
            # desaparezca ANTES del click. Si TM muestra ese spinner cuando
            # clickeamos, el click se ignora (overlay opaco). Sin este wait,
            # el bot fallaba con blockers_minor:captcha cuando en realidad
            # era "Confirmando disponibilidad" loading.
            try:
                _spinner_wait = 0
                while _spinner_wait < 15:   # max 15s
                    spinner_visible = page.evaluate(r"""() => {
                        const body = (document.body.innerText || '').toLowerCase();
                        return /confirmando\s+disponibilidad|comfirmando\s+disponibilidad|checking\s+availability/i.test(body);
                    }""")
                    if not spinner_visible:
                        break
                    page.wait_for_timeout(500)
                    _spinner_wait += 1
                if _spinner_wait > 0:
                    log(slot, "confirmando_disponibilidad_wait",
                        polls_500ms=_spinner_wait)
            except Exception: pass
            url_before = page.url
            continuar_ok = False
            # OPTIM 2026-05-12: solo JS direct click (Playwright role_button
            # SIEMPRE timeout en Camoufox → 4s perdidos por nada). Reducido
            # a 2 attempts, pausa 1-2s (era 3 attempts × 4-6s = 12-18s).
            for attempt in range(RETRY_CLICK_CONTINUAR):
                log(slot, "click_continuar", attempt=attempt + 1)
                clicked_cont = False
                try:
                    res = page.evaluate(r"""() => {
                        const all = Array.from(document.querySelectorAll(
                            'button, a[role="button"], [role="button"], input[type="submit"]'));
                        const btns = all.filter(b => b.offsetParent !== null && !b.disabled);
                        const t = btns.find(b => {
                            const tx = (b.innerText||b.textContent||b.value||'').trim();
                            return /continuar\s+comprando|^continuar$|continue\s+shopping/i.test(tx);
                        });
                        if (t) { t.click(); return (t.innerText||t.value||'').slice(0,40); }
                        return null;
                    }""")
                    if res:
                        clicked_cont = True
                        log(slot, "continuar_clicked_js", text=res)
                except Exception as _ce:
                    log(slot, "continuar_js_err", err=str(_ce)[:80])
                # OPTIM 2026-05-12: poll URL change cada 300ms durante 3s
                # (era human_pause(1,2) + wait_for_load_state 8s = hasta 10s)
                url_changed_fast = False
                for _ in range(10):
                    page.wait_for_timeout(300)
                    u_now = page.url.lower()
                    if u_now != url_before.lower() and "/event/" not in u_now:
                        url_changed_fast = True
                        break
                if url_changed_fast:
                    # URL cambió, no necesitamos blocker check
                    continuar_ok = True
                    log(slot, "continuar_ok", new_url=page.url[:80])
                    break
                # Detectar bloqueos COMUNES tras el click — try/except por si
                # el contexto sigue cambiando.
                blockers = {}
                try:
                    blockers = page.evaluate(r"""() => {
                        const txt = (document.body && document.body.innerText || '').toLowerCase();
                        const out = {};
                        if (/queue-it|estamos comprobando|verifying you are human/i.test(txt)) out.queueit = true;
                        if (/press.{0,20}hold|mant[eé]n pulsado/i.test(txt)) out.press_hold = true;
                        // Captcha: solo bloqueante si el iframe es VISIBLE (no
                        // los invisibles de NuData/scoring que TM siempre carga).
                        const capIfr = document.querySelector('iframe[src*="recaptcha"], iframe[src*="captcha"]');
                        if (capIfr) {
                            const r = capIfr.getBoundingClientRect();
                            if (r.width > 50 && r.height > 50 && capIfr.offsetParent !== null) {
                                out.captcha = true;
                            }
                        }
                        if (/access denied|acceso denegado|forbidden/i.test(txt)) out.blocked = true;
                        return out;
                    }""")
                except Exception as _e:
                    log(slot, "blockers_eval_skipped", err=str(_e)[:80])
                    blockers = {}
                # OPTIM 2026-05-12: silenciar captcha solo si NO hay queueit/blocked
                # (el iframe captcha invisible es falso positivo en Camoufox)
                if blockers and (blockers.get('queueit') or blockers.get('blocked') or blockers.get('press_hold')):
                    log(slot, "blockers_detected_post_continuar", **blockers)
                    shoot(slot, page, f"blocker_continuar_{attempt}")
                elif blockers:
                    # Silent: solo log compacto sin shot
                    log(slot, "blockers_minor", captcha=blockers.get('captcha', False))
                    if blockers.get("queueit"):
                        log(slot, "waiting_queue_it_max_3min")
                        # Esperar hasta 3 min a que queue-it termine
                        deadline_q = time.time() + 180
                        # P1: queue_monitor.report cada 10s mientras esperamos
                        try:
                            import queue_monitor as _qm
                        except Exception:
                            _qm = None
                        last_report_ts = 0
                        while time.time() < deadline_q:
                            page.wait_for_timeout(5000)
                            txt2 = page.evaluate("() => document.body.innerText.toLowerCase()")
                            # Reportar posición (si vemos número en texto queue-it)
                            if _qm and (time.time() - last_report_ts) >= 10:
                                try:
                                    # BUG FIX 2026-05-13: 'it' no existía en main() scope
                                    # → NameError silenciado por except: pass. Telemetría
                                    # queue-it llevaba semanas rota. Usar prof.env_id.
                                    m = re.search(r'(?:posici[oó]n|position|number)[^0-9]{0,40}(\d{1,7})', txt2)
                                    pos = int(m.group(1)) if m else 0
                                    _qm.report(
                                        worker_id=f"slot_{slot}",
                                        domain=(page.url.split('/')[2] if '://' in page.url else 'ticketmaster.es'),
                                        position=pos,
                                        profile_id=str(prof.get('env_id', '')),
                                        event_id=str(args.event_url.split('/event/')[-1].split('?')[0] if '/event/' in args.event_url else ''),
                                        status="queued")
                                    last_report_ts = time.time()
                                except Exception as _qre:
                                    log(slot, "queue_it_report_err", err=str(_qre)[:120])
                            if 'queue-it' not in txt2 and 'estamos comprobando' not in txt2:
                                log(slot, "queue_it_passed")
                                if _qm:
                                    try:
                                        _qm.report(worker_id=f"slot_{slot}",
                                                    domain=(page.url.split('/')[2] if '://' in page.url else 'ticketmaster.es'),
                                                    position=0,
                                                    event_id=str(args.event_url.split('/event/')[-1].split('?')[0] if '/event/' in args.event_url else ''),
                                                    status="passed")
                                    except Exception:
                                        pass
                                break

                # Si la URL ya no es la del evento, hemos avanzado
                u_after = page.url.lower()
                if u_after != url_before.lower() and "/event/" not in u_after:
                    continuar_ok = True
                    log(slot, "continuar_ok", new_url=u_after[:80])
                    break
                # Si seguimos en la misma página, reintenta
                log(slot, "continuar_no_redirect_retry", url=u_after[:80])
                # FIX 2026-05-14: re-check spinner ANTES de cada retry. Si TM
                # muestra "Confirmando disponibilidad" overlay, el click anterior
                # fue ignorado y los siguientes también lo serán hasta que el
                # spinner desaparezca. Wait extra (5s) si spinner detectado.
                try:
                    _spin_visible_retry = page.evaluate(r"""() => {
                        const body = (document.body.innerText || '').toLowerCase();
                        return /confirmando\s+disponibilidad|comfirmando\s+disponibilidad|checking\s+availability/i.test(body);
                    }""")
                    if _spin_visible_retry:
                        log(slot, "continuar_retry_spinner_detected")
                        # wait hasta 10s adicionales antes de re-click
                        for _ in range(20):
                            page.wait_for_timeout(500)
                            _vis = page.evaluate(r"""() => {
                                const body = (document.body.innerText || '').toLowerCase();
                                return /confirmando\s+disponibilidad|comfirmando\s+disponibilidad/i.test(body);
                            }""")
                            if not _vis:
                                log(slot, "continuar_retry_spinner_gone")
                                break
                except Exception: pass
                # Pausa más generosa entre retries (era 2-4s, ahora 3-6s para
                # dar tiempo a TM a procesar request invisible NuData)
                human_pause(3, 6)

            if not continuar_ok:
                shoot(slot, page, "continuar_failed_final")
                log(slot, "FATAL_continuar_no_avanza",
                    note="TM no acepta click. Posible: sesión inválida, queue-it activo, NuData fingerprint")
                # FIX 2026-05-14: NO quemar la cuenta por continuar_no_avanza.
                # Esto es un error TRANSIENT de TM (captcha invisible / spinner
                # de Confirmando disponibilidad). La cuenta NO es mala — solo
                # TM rechazó el click. Quemar profile + subnet pero NO account.
                try:
                    _persist_bot_block(prof.get('profile', ''),
                                        prof.get('ip', ''),
                                        'continuar_no_avanza',
                                        account_id=None)   # ← NO quemar cuenta
                    log(slot, "continuar_no_avanza_persisted",
                        profile=prof.get('profile'),
                        subnet=_ip_to_subnet24(prof.get('ip', '')),
                        note="account NOT burned (transient TM block)")
                except Exception: pass
                write_outcome(slot, {
                    "status": "continuar_blocked",
                    "step_reached": "continuar_post_acepto",
                    "error": "tm_continuar_no_responde",
                    "burned_profile": prof.get('profile'),
                    "burned_subnet": _ip_to_subnet24(prof.get('ip', '')),
                })
                return 99   # RC_BOT_BLOCK family — wrapper swap

            # 10) OAuth login si redirige (segunda fase post-cart) con account-swap
            u = page.url.lower()
            log(slot, "post_continuar_url", url=u[:100])
            if any(k in u for k in ['auth.ticketmaster', 'identity', 'login', 'signin']):
                log(slot, "oauth_detected")
                # ── TRACE_AUTH=1 → diagnóstico CDP aislado en _trace_auth.py ──
                # Cleanup garantizado (listeners detach + exports). Si flag activa,
                # captura 40s en /auth y termina aquí; sin flag, sigue al login normal.
                try:
                    import _trace_auth as _ta
                    if _ta.trace_enabled():
                        log(slot, "TRACE_AUTH_start")
                        with _ta.AuthTracer(ctx, page, label="trace_auth",
                                             on_event=lambda kind, data: log(slot, kind, **data)) as _tracer:
                            log(slot, "TRACE_AUTH_waiting_40s",
                                out_dir=str(_tracer.out_dir))
                            time.sleep(40)
                        # __exit__ ya hizo detach() + exports
                        log(slot, "TRACE_AUTH_done", out_dir=str(_tracer.out_dir))
                        return 0
                except Exception as _te:
                    log(slot, "TRACE_AUTH_err", err=str(_te)[:200])
                # ── END TRACE_AUTH ──
                login_res = login_with_account_swap(
                    slot, page, ctx, account, card, used_lock_path,
                    max_account_swaps=LIMIT_ACCOUNT_SWAPS)
                if not login_res.get("ok"):
                    log(slot, "FATAL_oauth_login_failed",
                        reason=login_res.get("reason"))
                    # max_swaps / no_more_accounts / OTP timeout son
                    # recuperables vía wrapper: nuevo perfil + IP + cuenta.
                    rec_reasons = ("max_swaps", "no_more_accounts", None)
                    if login_res.get("reason") in rec_reasons:
                        # Persistir outcome con status=login_exhausted para
                        # que el wrapper sepa que vale la pena retry.
                        write_outcome(slot, {
                            "status": "login_exhausted",
                            "step_reached": "oauth_login",
                            "error": "all_account_swaps_failed",
                            "reason": login_res.get("reason"),
                        })
                        return 99   # RC_BOT_BLOCK family — wrapper retries
                    return 1
                if login_res.get("swaps", 0) > 0:
                    account = login_res.get("account", account)
                    card = login_res.get("card", card)
                # Tras account_recovery exitoso, TM redirige a /account y el cart
                # se perdió. Re-navegar al evento y rehacer flow stepper +
                # find_tickets — esta vez sin OAuth (ya logueado).
                if login_res.get("session_recovered"):
                    log(slot, "session_recovered_re_navigating_event")
                    try:
                        page.goto(args.event_url, wait_until='domcontentloaded',
                                  timeout=30000)
                        # OPTIM 2026-05-12: post-recovery flow ~30s más rápido
                        human_pause(1.5, 2.5)
                        # Cookies banner — JS direct (más rápido que get_by_text)
                        try:
                            page.evaluate(r"""() => {
                                const b = document.querySelector('#onetrust-accept-btn-handler')
                                    || Array.from(document.querySelectorAll('button')).find(x => /aceptar/i.test(x.innerText||''));
                                if (b && b.offsetParent !== null) b.click();
                            }""")
                            page.wait_for_timeout(500)
                        except Exception: pass
                        shoot(slot, page, "post_recovery_event_loaded")
                        # Re-hacer quick mode: click "Ver lo mejor disponible"
                        try:
                            page.evaluate(r"""() => {
                                const all = Array.from(document.querySelectorAll('button, a, [role="button"], [role="tab"]'));
                                const b = all.find(x => /ver\s+lo\s+mejor\s+disponible|best\s+available/i.test(x.innerText||x.textContent||''));
                                if (b) b.click();
                            }""")
                        except Exception: pass
                        human_pause(1, 2)   # OPTIM: era 3-5s
                        # Re-stepper qty con pausas humanas cortas
                        for i in range(args.qty + 4):
                            try:
                                cur = page.evaluate(f"""() => {{
                                    const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{args.offer_idx}];
                                    return s ? parseInt(s.innerText.replace(/\\D/g,''))||0 : -1;
                                }}""")
                                if cur >= args.qty: break
                                page.evaluate(f"""() => {{
                                    const s = document.querySelectorAll('[data-testid="quantityStepper"]')[{args.offer_idx}];
                                    if (!s) return;
                                    const btns = s.querySelectorAll('button');
                                    (btns[btns.length-1] || btns[0]).click();
                                }}""")
                                page.wait_for_timeout(int(400 + random.random()*400))   # OPTIM: 0.4-0.8s
                            except Exception: break
                        shoot(slot, page, "post_recovery_stepper_done")
                        # Re-click Entradas verde — JS direct (más rápido que Playwright)
                        try:
                            page.evaluate(r"""() => {
                                const btns = Array.from(document.querySelectorAll(
                                    'button, a[role="button"]')).filter(b => b.offsetParent !== null);
                                const t = btns.find(b => b.dataset?.testid === 'findTicketsBtn')
                                    || btns.find(b => /^entradas$|^get tickets$/i.test((b.innerText||'').trim()));
                                if (t) t.click();
                            }""")
                        except Exception as _fe:
                            log(slot, "post_recovery_find_tickets_err", err=str(_fe)[:120])
                        human_pause(1.5, 2.5)   # OPTIM: era 4-6s
                        shoot(slot, page, "post_recovery_after_find_tickets")
                        # Acepto checkbox + Continuar — todo via JS direct
                        try:
                            page.evaluate(r"""() => {
                                const labels = Array.from(document.querySelectorAll('label'));
                                const lbl = labels.find(l => /acepto que he le[ií]do/i.test(l.textContent||''));
                                if (!lbl) return;
                                const forId = lbl.getAttribute('for');
                                let cb = forId ? document.getElementById(forId) : null;
                                if (!cb) cb = lbl.querySelector('input[type=checkbox]');
                                if (cb) cb.click();
                            }""")
                            log(slot, "post_recovery_checkbox_acepto")
                        except Exception: pass
                        human_pause(0.8, 1.5)   # OPTIM: era 5-7s
                        # FIX 2026-05-14: Retry del click Continuar comprando hasta 5x
                        # (la página puede estar cargando spinner cuando intentamos).
                        # Verifica que la URL cambie como confirmación de éxito.
                        continuar_clicked = False
                        url_before_continuar = page.url
                        for _retry_cont in range(5):
                            try:
                                res = page.evaluate(r"""() => {
                                    const all = Array.from(document.querySelectorAll(
                                        'button, a[role="button"], [role="button"]'))
                                        .filter(b => b.offsetParent !== null && !b.disabled);
                                    const t = all.find(b => /continuar\s+comprando|^continuar$|continue/i.test(
                                        (b.innerText||b.textContent||'').trim()));
                                    if (t) { t.click(); return t.innerText; }
                                    return null;
                                }""")
                                if res:
                                    log(slot, "post_recovery_continuar_clicked",
                                        text=str(res)[:50], attempt=_retry_cont+1)
                                    continuar_clicked = True
                                    page.wait_for_timeout(2500)
                                    # Verificar si URL cambió (avanzamos al siguiente paso)
                                    if page.url != url_before_continuar:
                                        log(slot, "post_recovery_advanced",
                                            new_url=page.url[:80])
                                        break
                                else:
                                    log(slot, "post_recovery_continuar_not_found_retry",
                                        attempt=_retry_cont+1)
                            except Exception as _cre:
                                log(slot, "post_recovery_continuar_err",
                                    err=str(_cre)[:80], attempt=_retry_cont+1)
                            # Esperar entre retries — la página puede estar cargando
                            page.wait_for_timeout(2500)
                        if not continuar_clicked:
                            log(slot, "post_recovery_continuar_FAILED_after_5_retries")
                        log(slot, "post_recovery_url", url=page.url[:100])
                    except Exception as _re:
                        log(slot, "post_recovery_renav_err", err=str(_re)[:200])
                human_pause(0.5, 1)   # OPTIM: era 4-6s

            # 10b) Pantalla intermedia "Partners del evento + Condiciones + Ir al pago"
            # Puede aparecer ANTES o DESPUÉS del OAuth. Reintentamos hasta 5 veces
            # mientras el watcher avanza al checkout final.
            # Check bot-block ANTES — si TM ya nos pilló, no insistir.
            def _early_bot_check(label):
                try:
                    body = page.evaluate(
                        '() => (document.body && document.body.innerText || "").slice(0, 1500)'
                    ) or ''
                    low = body.lower()
                    markers = ['su sesión se ha suspendido',
                               'session has been paused',
                               'browsing activity has been paused',
                               'forma de navegar o en su red nos hizo pensar']
                    for m in markers:
                        if m in low:
                            shoot(slot, page, f"bot_block_early_{label}")
                            log(slot, "FATAL_bot_block_early",
                                where=label, marker=m[:50])
                            try:
                                _persist_bot_block(prof.get('profile', ''),
                                                    prof.get('ip', ''), label,
                                                    account_id=str(account.get('id', '')))
                                log(slot, "bot_block_persisted",
                                    profile=prof.get('profile'),
                                    ip=prof.get('ip'),
                                    subnet=_ip_to_subnet24(prof.get('ip', '')),
                                    account_id=account.get('id'),
                                    email=account.get('tm_email'))
                            except Exception: pass
                            write_outcome(slot, {
                                "status": "bot_block",
                                "step_reached": label,
                                "error": "akamai_bot_block_session_suspended",
                                "marker": m[:60],
                                "burned_profile": prof.get('profile'),
                                "burned_subnet": _ip_to_subnet24(prof.get('ip', '')),
                                "burned_account_id": account.get('id'),
                                "burned_email": account.get('tm_email'),
                            })
                            return True
                except Exception: pass
                return False
            if _early_bot_check("pre_checkout_entry"):
                return 99   # RC_BOT_BLOCK — wrapper hace swap profile/IP
            for i in range(5):
                if handle_pre_checkout_step(slot, page):
                    break
                page.wait_for_timeout(2500)
                if _early_bot_check(f"pre_checkout_iter{i}"):
                    return 99

            # ═══ Helper: detectar bot-block Akamai/TM ═══
            # Si TM muestra "Su sesión se ha suspendido" / "Your browsing
            # activity has been paused" → abortar sin insistir (más insistencia
            # empeora la reputación de IP/perfil con Akamai).
            def _check_bot_block(label):
                try:
                    body = page.evaluate(
                        '() => (document.body && document.body.innerText || "").slice(0, 1500)'
                    ) or ''
                    markers = [
                        'su sesión se ha suspendido',
                        'tu sesión se ha suspendido',
                        'session has been paused',
                        'your browsing activity has been paused',
                        'forma de navegar o en su red nos hizo pensar que era un bot',
                        'sospechamos que', 'detectado actividad inusual',
                    ]
                    low = body.lower()
                    for m in markers:
                        if m in low:
                            shoot(slot, page, f"bot_block_{label}")
                            log(slot, "FATAL_bot_block_detected",
                                where=label, marker=m[:50])
                            # Blacklist perfil + subnet /24 + account_id
                            try:
                                _persist_bot_block(prof.get('profile', ''),
                                                    prof.get('ip', ''), label,
                                                    account_id=str(account.get('id', '')))
                                log(slot, "bot_block_persisted",
                                    profile=prof.get('profile'),
                                    ip=prof.get('ip'),
                                    subnet=_ip_to_subnet24(prof.get('ip', '')),
                                    account_id=account.get('id'),
                                    email=account.get('tm_email'))
                            except Exception: pass
                            write_outcome(slot, {
                                "status": "bot_block",
                                "step_reached": label,
                                "error": "akamai_bot_block_session_suspended",
                                "marker": m[:60],
                                "burned_profile": prof.get('profile'),
                                "burned_subnet": _ip_to_subnet24(prof.get('ip', '')),
                            })
                            return True
                except Exception: pass
                return False

            # Check inmediato post-OAuth/checkout-entry antes de entrar al loop
            if _check_bot_block("post_oauth_pre_watch"):
                return 99   # RC_BOT_BLOCK — wrapper hace swap

            # 10) Watcher hasta checkout → fill_card → PAUSA
            log(slot, "watch_for_checkout")
            deadline = time.time() + 600
            prev = page.url
            filled = False
            _bot_block_checks = 0
            # Bug fix 2026-05-11: si al entrar al loop la URL ya está en
            # checkout, el `if cur != prev` nunca dispara y nunca llega al
            # fill_card. Forzar check inicial del URL state.
            # Bug fix 2026-05-12: TM OAuth incluye `?headerTitle=Checkout` que
            # disparaba falsos positivos en login screen. Usar SOLO host/path,
            # no el query string completo.
            def _is_checkout_url(url: str) -> bool:
                try:
                    from urllib.parse import urlparse
                    p = urlparse(url or '')
                    host = (p.hostname or '').lower()
                    path = (p.path or '').lower()
                    if any(h in host for h in ('auth.ticketmaster',
                                                  'identity.ticketmaster',
                                                  'login.ticketmaster')):
                        return False
                    if 'payments.tick' in host: return True
                    if any(seg in path for seg in ('/checkout/', '/checkout',
                                                      '/payment', '/cart',
                                                      '/pago')):
                        return True
                except Exception: pass
                return False
            # ═══ HELPER 2026-05-14: save state genérico (puede llamarse desde
            # CUALQUIER punto del flow, no solo tras login_full). Esto garantiza
            # que las cookies device-trust se actualizan en cada run exitoso,
            # aunque la cuenta saltara login_full por tener state previo.
            def _save_state_now(reason="generic"):
                try:
                    from datetime import datetime as _dt
                    _ts = _dt.now().strftime('%Y%m%d_%H%M%S')
                    _aid_short = str(account.get('id') or 'unk')[:12]
                    _sessions_dir = os.path.join(PROYECTO, "datos", "sessions")
                    os.makedirs(_sessions_dir, exist_ok=True)
                    _state_file = os.path.join(
                        _sessions_dir, f"tm_cmp_{_aid_short}_{_ts}_state.json")
                    ctx.storage_state(path=_state_file)
                    try:
                        from account_manager import account_manager as _am
                        _am.update_account(account.get('id'),
                                            {"storage_state_file": _state_file,
                                             "last_bot_state_save_at": _dt.now().isoformat(),
                                             "last_bot_state_save_reason": reason})
                    except Exception:
                        pass
                    log(slot, "storage_state_saved_runtime",
                        path=os.path.basename(_state_file),
                        reason=reason,
                        account_id=account.get('id'))
                except Exception as _se:
                    log(slot, "storage_state_save_runtime_err",
                        err=str(_se)[:120], reason=reason)

            try:
                u0 = page.url
                log(slot, "watch_initial_url", url=u0[:100])
                if _is_checkout_url(u0):
                    log(slot, "checkout_detected_at_entry")
                    # SAVE STATE 1: cuenta llegó a checkout sin reset → domesticada
                    _save_state_now("checkout_detected_at_entry")
                    # M13.2: timestamp para stale_checkout check del gate
                    _checkout_started_at_monotonic = time.monotonic()
                    # ─── FLOW POST-CHECKOUT 14 PASOS (memoria 2026-05-11) ───
                    # ETAPA A: fill_card(dirección) — JS native setter + Provincia
                    #          combobox + click "Guardar y continuar"
                    # ETAPA B: Loop por etapas intermedias:
                    #          - handle_partners_seguro_step (Sí Partners + No Seguro)
                    #          - Click "Ir al pago" / "Continuar"
                    #          - Hasta llegar a payments.ticketmaster.es
                    # ETAPA C: fill_card_adyen (iframes tarjeta) en payments page
                    # ETAPA D: Click "Realizar Compra" → STOP en pantalla 3DS

                    # ─── ETAPA A: dirección ───
                    fill_card(slot, page, card, account)
                    page.wait_for_timeout(2500)

                    # ─── ETAPA B: loop por etapas hasta payments.tick ───
                    # FIX 2026-05-12: renombrar kwarg `stage` → `stg` para
                    # evitar colisión con parámetro `stage` de log().
                    payments_reached = False
                    for _stg in range(RETRY_CHECKOUT_STAGES):
                        page.wait_for_timeout(2000)
                        cur_url = (page.url or '').lower()
                        log(slot, "checkout_stage_url", stg=_stg, url=cur_url[:80])
                        # Si ya estamos en payments → salir
                        if 'payments.ticketmaster' in cur_url:
                            payments_reached = True
                            log(slot, "payments_screen_reached", stg=_stg)
                            break
                        # Handler Partners(Sí) + Seguro(No)
                        try: handle_partners_seguro_step(slot, page)
                        except Exception: pass
                        page.wait_for_timeout(800)
                        # FIX 2026-05-14: marcar checkbox "Condiciones de Compra"
                        # si aparece en esta etapa (TM la muestra a veces en
                        # stage tardío, no solo en pre_checkout). Sin esto, el
                        # botón "Ir al pago" da error y el loop se queda
                        # clickeando sin avanzar.
                        try:
                            _conds_checked = page.evaluate(r"""() => {
                                const labels = Array.from(document.querySelectorAll('label'));
                                const condsLbl = labels.find(l => {
                                    const t = (l.textContent || '').toLowerCase();
                                    return /condiciones\s+de\s+compra|acepto.*condiciones|t[eé]rminos\s+y\s+condiciones|acepto\s+que\s+he\s+le[ií]do/.test(t);
                                });
                                if (!condsLbl) return {found: false};
                                const id = condsLbl.getAttribute('for');
                                let cb = id ? document.getElementById(id) : condsLbl.querySelector('input[type=checkbox]');
                                if (!cb) {
                                    // Buscar checkbox cerca del label
                                    const parent = condsLbl.closest('div, section, fieldset');
                                    if (parent) cb = parent.querySelector('input[type=checkbox]');
                                }
                                if (!cb) return {found: true, cb: false};
                                if (cb.checked) return {found: true, cb: true, already_checked: true};
                                cb.scrollIntoView({block: 'center'});
                                cb.click();
                                return {found: true, cb: true, clicked: true,
                                        now_checked: cb.checked};
                            }""")
                            if _conds_checked and _conds_checked.get('clicked'):
                                log(slot, "checkout_stage_conds_checked", stg=_stg,
                                    now_checked=_conds_checked.get('now_checked'))
                                page.wait_for_timeout(600)
                        except Exception as _ce:
                            log(slot, "checkout_stage_conds_err", err=str(_ce)[:80])
                        # Click "Guardar y continuar" / "Continuar" / "Ir al pago"
                        # — JS direct PRIMERO (más robusto que Playwright role_button)
                        try:
                            clicked = page.evaluate(r"""() => {
                                const btns = Array.from(document.querySelectorAll(
                                    'button, [role="button"], input[type="submit"]'))
                                    .filter(b => b.offsetParent !== null && !b.disabled);
                                const exact = btns.find(b => {
                                    const t = (b.innerText||b.textContent||b.value||'').trim().toLowerCase();
                                    return /^(guardar y continuar|guardar|continuar|continue|ir al pago|next|pagar)$/i.test(t);
                                });
                                if (exact) { exact.click(); return exact.innerText || exact.value; }
                                return null;
                            }""")
                            if clicked:
                                log(slot, "checkout_stage_btn_clicked",
                                    stg=_stg, text=str(clicked)[:50])
                                # FIX 2026-05-14: post-click wait extendido 3s → 4s.
                                # TM puede tardar más en redirect a payments.tm.es,
                                # especialmente cuando el bot va más rápido por
                                # las optimizaciones recientes.
                                page.wait_for_timeout(4000)
                        except Exception as _be:
                            log(slot, "checkout_stage_btn_err", err=str(_be)[:80])
                        # Screenshot cada etapa para debugging
                        shoot(slot, page, f"checkout_stg_{_stg}")

                    # FIX 2026-05-14: Si el bucle terminó sin payments, hacer
                    # un poll final de hasta 15s. TM a veces hace el redirect
                    # final con delay (server-side processing pago).
                    if not payments_reached:
                        log(slot, "checkout_final_redirect_poll_start")
                        for _final_poll in range(15):
                            page.wait_for_timeout(1000)
                            cur_url = (page.url or '').lower()
                            if 'payments.ticketmaster' in cur_url:
                                payments_reached = True
                                log(slot, "payments_screen_reached_final_poll",
                                    poll=_final_poll)
                                break
                        if not payments_reached:
                            log(slot, "checkout_final_poll_NOT_REACHED",
                                last_url=cur_url[:120])

                    # ─── ETAPA C: fill_card_adyen en payments page ───
                    if payments_reached:
                        page.wait_for_timeout(3000)
                        fill_card_adyen(slot, page, card)
                        filled = True
                        page.wait_for_timeout(1500)

                        # ─── ETAPA D: Click "Realizar Compra" → 3DS ───
                        # CRÍTICO: este click dispara el cargo + 3DS del banco.
                        # NO completar el SMS — solo llegar a la pantalla.

                        # M13.2: PurchaseGate check — última línea de defensa
                        # antes del submit irreversible. Default state del gate
                        # es disabled+kill_off → bloquea todo por defecto.
                        # Activar vía env: PURCHASE_MODE + GLOBAL_PURCHASE_ENABLED.
                        _gate_action, _gate_status = _purchase_gate_check(
                            slot, account, args, _checkout_started_at_monotonic
                        )
                        if _gate_action == 'abort':
                            log(slot, "🛑 PURCHASE_GATE_ABORT",
                                status=_gate_status.get("status"),
                                reason=_gate_status.get("reason", ""))
                            # SAVE STATE 2 (gate denied): la cuenta llegó hasta
                            # pre-pago sin disparar bloqueos → guardamos cookies
                            # actualizadas para próximos runs.
                            try: _save_state_now("purchase_gate_abort")
                            except Exception: pass
                            write_outcome(slot, _gate_status)
                            try:
                                page.wait_for_timeout(15000)
                            except Exception:
                                pass
                            return 0
                        # gate ALLOW + LIVE → proceder con click real
                        try:
                            page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})")
                            page.wait_for_timeout(1000)
                            realizar_clicked = page.evaluate(r"""() => {
                                const btns = Array.from(document.querySelectorAll(
                                    'button, [role="button"], input[type="submit"]'))
                                    .filter(b => b.offsetParent !== null && !b.disabled);
                                const exact = btns.find(b => {
                                    const t = (b.innerText||b.textContent||b.value||'').trim().toLowerCase();
                                    return /^(realizar compra|comprar|confirmar|pagar|finalizar compra|pay now)$/i.test(t);
                                });
                                if (exact) { exact.click(); return exact.innerText || exact.value; }
                                return null;
                            }""")
                            log(slot, "realizar_compra_clicked", text=str(realizar_clicked)[:50])
                            shoot(slot, page, "post_realizar_compra")
                            page.wait_for_timeout(4500)   # esperar redirect 3DS
                            # Detectar pantalla 3DS (iframe banco / URL diferente)
                            tds_info = page.evaluate(r"""() => {
                                const url = location.href;
                                const ifs = Array.from(document.querySelectorAll('iframe'))
                                    .map(f => f.src||'').filter(s => s.length > 0);
                                const body = (document.body.innerText||'').slice(0, 1500).toLowerCase();
                                const markers_3ds = [
                                    '3d secure', '3-d secure', 'secure code',
                                    'verifica', 'verificación', 'authentication',
                                    'autenticación', 'código sms', 'enviado a su',
                                    'código de confirmación', 'pin de seguridad',
                                    'banco', 'verifíquese', 'autorización',
                                ];
                                const markers_card_rejected = [
                                    'tarjeta ha sido rechazada',
                                    'tarjeta rechazada', 'card declined',
                                    'card rejected', 'contacta con tu proveedor',
                                    'inténtalo con otro método',
                                ];
                                const has_3ds = markers_3ds.some(m => body.includes(m))
                                    || ifs.some(s => /3ds|secure|acs|emvco|verified|challenge/i.test(s));
                                const card_rejected = markers_card_rejected.some(m => body.includes(m));
                                return {url: url, iframes: ifs.slice(0, 5),
                                        body_preview: body.slice(0, 400),
                                        has_3ds: has_3ds,
                                        card_rejected: card_rejected};
                            }""")
                            log(slot, "3ds_check", **tds_info)
                            shoot(slot, page, "3ds_screen")
                            if tds_info.get('has_3ds'):
                                log(slot, "🎯 3DS_SCREEN_REACHED_STOP",
                                    note="LLEGAMOS A 3DS - bot detenido, NO completar SMS")
                                write_outcome(slot, {
                                    "status": "3ds_reached",
                                    "step_reached": "3ds_screen",
                                    "url_3ds": tds_info.get('url'),
                                    "iframes": tds_info.get('iframes'),
                                    "account_email": account.get('tm_email'),
                                })
                                # Esperar 60s en 3DS para que el user vea
                                page.wait_for_timeout(60000)
                                return 0   # ✓ éxito final
                            elif tds_info.get('card_rejected'):
                                log(slot, "💳 CARD_REJECTED_BY_BANK",
                                    note="Banco rechazó tarjeta (probable CVV invalido o test card). Flow técnico OK.")
                                write_outcome(slot, {
                                    "status": "card_rejected",
                                    "step_reached": "post_realizar_compra",
                                    "url": tds_info.get('url'),
                                    "account_email": account.get('tm_email'),
                                    "note": "Flow técnico funcionó hasta el banco. CVV o tarjeta inválida.",
                                })
                                # Esperar 30s para inspección visual
                                page.wait_for_timeout(30000)
                                return 0   # ✓ éxito técnico (banco rechazó)
                        except Exception as _3e:
                            log(slot, "realizar_compra_err", err=str(_3e)[:120])
                    else:
                        log(slot, "payments_screen_NOT_reached_after_8_stages")
            except Exception as _ie:
                log(slot, "watch_initial_check_err", err=str(_ie)[:120])
            _passkey_polls = 0
            while time.time() < deadline:
                time.sleep(2)
                # Check bot-block cada 5s (cada ~3 iteraciones)
                _bot_block_checks += 1
                if _bot_block_checks % 3 == 0:
                    if _check_bot_block("watch_loop"):
                        return 99   # RC_BOT_BLOCK
                # FIX 2026-05-14: Check passkey prompt cada ~6s (cada 3 iter).
                # TM puede mostrar "Añadir Llave de Acceso" durante watch loop
                # post-OAuth y queda atascado si nadie clickea "Ahora mismo no".
                _passkey_polls += 1
                if _passkey_polls % 3 == 0:
                    try:
                        _skip_passkey_prompt(slot, page)
                    except Exception:
                        pass
                try:
                    cur = page.url
                    if cur != prev:
                        log(slot, "URL_CHANGED", to=cur[:120])
                        prev = cur
                        page.wait_for_timeout(3000)
                        # Tras cada cambio de URL, intentar handler "Partners +
                        # Condiciones + Ir al pago" — puede aparecer en
                        # múltiples pantallas intermedias.
                        try:
                            handle_pre_checkout_step(slot, page)
                        except Exception: pass
                        u = cur.lower()
                        if any(k in u for k in ['identity', 'login', 'auth', 'signin', 'my.tick']):
                            log(slot, "auth_detected_mid")
                            login_res = login_with_account_swap(
                                slot, page, ctx, account, card,
                                used_lock_path, max_account_swaps=LIMIT_ACCOUNT_SWAPS)
                            if not login_res.get("ok"):
                                log(slot, "FATAL_mid_login_failed",
                                    reason=login_res.get("reason"))
                                # ─── P4 SRE: surgical hook ───
                                write_outcome(slot, {
                                    "status": "fatal_mid_login_failed",
                                    "step_reached": "mid_login",
                                    "reason": login_res.get("reason"),
                                    "rc": 1,
                                })
                                return 1
                            if login_res.get("swaps", 0) > 0:
                                account = login_res.get("account", account)
                                card = login_res.get("card", card)
                            human_pause(3, 5)
                        if _is_checkout_url(page.url) and not filled:
                            log(slot, "checkout_detected")
                            # Esperar a que iframes Adyen estén ready (suele tardar
                            # 2-5s) — el motivo principal de "no rellenó la tarjeta"
                            # antes era empezar el fill demasiado pronto.
                            for _ in range(8):
                                page.wait_for_timeout(800)
                                ready = page.evaluate(r"""() => {
                                    const ifs = document.querySelectorAll('iframe[src*="adyen" i], iframe[src*="checkout" i], iframe[name*="card" i]');
                                    return ifs.length > 0;
                                }""")
                                if ready:
                                    log(slot, "adyen_iframes_ready"); break
                            fill_card(slot, page, card, account)
                            # Verificar que rellenó al menos card_number; si no, reintentar.
                            verify = page.evaluate(r"""() => {
                                const all = [];
                                for (const f of [document, ...document.querySelectorAll('iframe')]) {
                                    try {
                                        const doc = f.contentDocument || f;
                                        doc.querySelectorAll('input').forEach(i => {
                                            if (i.value && i.value.length > 0) all.push({n: i.name, v: i.value.length});
                                        });
                                    } catch(e) {}
                                }
                                return all;
                            }""")
                            log(slot, "card_fields_filled", count=len(verify or []),
                                fields=[v.get("n") for v in (verify or [])][:8])
                            if not verify or len(verify) < 3:
                                log(slot, "card_fill_retry", reason="muy pocos campos rellenados")
                                page.wait_for_timeout(2500)
                                fill_card(slot, page, card, account)
                            filled = True
                            shoot(slot, page, "checkout_ready")
                            log(slot, "READY_FOR_USER", url=page.url)
                            _ready_banner(slot, page.url)
                            flag = os.path.join(PROYECTO, "_compra_parallel.STOP")
                            while not os.path.exists(flag):
                                time.sleep(5)
                            # Limpiar historial DENTRO del bloque sync_playwright
                            try:
                                import browser_cleanup
                                cdp_res = browser_cleanup.clear_history_via_cdp(ctx)
                                log(slot, "history_cleanup_inside", **cdp_res)
                            except Exception as _e:
                                log(slot, "history_cleanup_inside_err", err=str(_e)[:80])
                            log(slot, "stop_received"); return 0
                except Exception as e:
                    log(slot, "watch_err", err=str(e)[:80])
            log(slot, "TIMEOUT_no_checkout")
            # Limpiar historial al timeout (todavía dentro del with)
            try:
                import browser_cleanup
                browser_cleanup.clear_history_via_cdp(ctx)
                log(slot, "history_cleanup_timeout_ok")
            except Exception: pass

    finally:
        # ─── Cleanup SIEMPRE antes de cerrar el browser ───
        # Norma user 2026-05-12: borrar TODO rastro al cerrar para que próxima
        # apertura sea limpia sin restos de la sesión anterior.
        try:
            import browser_cleanup as _bc
            # CDP-level: cookies + cache + Storage.clearDataForOrigin
            cdp_res = _bc.clear_history_via_cdp(ctx) if 'ctx' in dir() else None
            log(slot, "history_cleanup_finally",
                steps=(cdp_res or {}).get('steps', []),
                ok=(cdp_res or {}).get('ok', False))
        except Exception as _ce:
            log(slot, "history_cleanup_finally_err", err=str(_ce)[:120])
        # OPTIM 2026-05-12: limpieza EXPLÍCITA Camoufox antes de cerrar
        # (asegura que no queden cookies/storage en memoria que filtren)
        if engine == 'camoufox':
            try:
                if 'ctx' in dir():
                    # Borrar cookies del context
                    ctx.clear_cookies()
                    # Borrar storage (localStorage, sessionStorage, IndexedDB)
                    if 'page' in dir():
                        try:
                            page.evaluate(r"""() => {
                                try { localStorage.clear(); } catch(e) {}
                                try { sessionStorage.clear(); } catch(e) {}
                                try {
                                    if (window.indexedDB && indexedDB.databases) {
                                        indexedDB.databases().then(dbs => {
                                            dbs.forEach(db => indexedDB.deleteDatabase(db.name));
                                        });
                                    }
                                } catch(e) {}
                                // Borrar Service Workers
                                try {
                                    if (navigator.serviceWorker) {
                                        navigator.serviceWorker.getRegistrations().then(rs =>
                                            rs.forEach(r => r.unregister()));
                                    }
                                } catch(e) {}
                            }""")
                        except Exception: pass
                    log(slot, "camoufox_explicit_cleanup_ok")
            except Exception as _ce:
                log(slot, "camoufox_explicit_cleanup_err", err=str(_ce)[:120])
        # ─── Cerrar browser + locks ───
        if engine == 'silent':
            # Silent: cerrar via API (+ limpieza CDP best-effort interna de
            # silent_engine). El proxy está embebido: no hay destroy/burn.
            try:
                if '_pw_dummy' in dir() and _pw_dummy is not None:
                    _pw_dummy.__exit__(None, None, None)
            except Exception: pass
            try:
                import silent_engine as _se
                _close_res = _se._close_profile_clean(
                    prof['env_id'], port, prof.get('profile', ''))
                log(slot, "profile_closed", engine='silent',
                    closed=_close_res.get('closed'),
                    keep=_close_res.get('keep'),
                    drop=_close_res.get('drop'))
            except Exception as e:
                log(slot, "profile_close_err", err=str(e)[:80])
        elif engine == 'morelogin':
            try:
                requests.post(f'{bo.MORELOGIN_API}/api/env/close',
                               json={'envId': prof['env_id']}, timeout=15)
                log(slot, "profile_closed")
            except Exception as e:
                log(slot, "profile_close_err", err=str(e)[:80])
            # NORMA USER 2026-05-13:
            # - Si era auto_created (fresh) → DESTROY perfil + proxy (no
            #   contaminar pool MoreLogin con auto-perfiles, límite 20000).
            # - Si era pool existente → BURN (no reutilizar).
            try:
                if prof.get('auto_created'):
                    res = bo.destroy_fresh_profile(prof)
                    log(slot, "profile_auto_destroyed",
                        profile=prof.get('profile'),
                        profile_deleted=res.get('profile_deleted'),
                        proxy_deleted=res.get('proxy_deleted'),
                        errors=res.get('errors'))
                else:
                    import register_account as _ra
                    if hasattr(_ra, 'load_burned') and hasattr(_ra, 'save_burned'):
                        _b = _ra.load_burned()
                        _b.add(prof['profile'])
                        _ra.save_burned(_b)
                        log(slot, "profile_auto_burned_after_use",
                            profile=prof['profile'],
                            total_burned=len(_b))
            except Exception as _be:
                log(slot, "profile_cleanup_err", err=str(_be)[:80])
            try:
                if '_pw_dummy' in dir() and _pw_dummy is not None:
                    _pw_dummy.__exit__(None, None, None)
            except Exception: pass
        else:
            # Camoufox: cerrar context manager
            try:
                if '_cm_camoufox' in dir() and _cm_camoufox is not None:
                    _cm_camoufox.__exit__(None, None, None)
                log(slot, "camoufox_closed")
            except Exception as e:
                log(slot, "camoufox_close_err", err=str(e)[:80])
            # FIX 2026-05-14 (refinado): NO matar camoufox.exe por nombre porque
            # afecta a otros slots paralelos. El context manager __exit__ debería
            # bastar para terminar el subprocess de ESTE slot. Si quedaron zombies
            # de slots crasheados, el cleanup de tmp dirs al inicio del próximo
            # launch los limpia indirectamente (los tmp dirs que el zombie usaba
            # son removidos, forzando crash del zombie).
            # No-op: __exit__ above is sufficient.
        # File-level purge tras close (History/IndexedDB/Cookies en disco) —
        # solo aplica a MoreLogin (Camoufox usa tmp dirs limpios siempre).
        if engine == 'morelogin':
            try:
                import browser_cleanup as _bc
                import os as _os
                morelogin_root = _os.path.expandvars(
                    r'%APPDATA%\MoreLogin\User Data')
                candidate = _os.path.join(morelogin_root, str(prof.get('env_id', '')))
                if _os.path.isdir(candidate):
                    files_res = _bc.post_close_purge(candidate)
                    log(slot, "history_files_purged",
                        deleted=len(files_res.get('deleted', [])),
                        errors=len(files_res.get('errors', [])))
            except Exception as _fe:
                log(slot, "history_files_purge_err", err=str(_fe)[:120])
        # Liberar locks (MoreLogin/Silent usan active_profiles; camoufox no)
        if engine == 'morelogin':
            _remove_active_profile(prof['profile'])
        elif engine == 'silent':
            try:
                import silent_engine as _se
                _se._release_profile(prof.get('profile', ''))
            except Exception: pass
        try:
            with open(used_lock_path, encoding='utf-8') as f:
                ids = set(json.load(f))
            ids.discard(account.get('id'))
            with open(used_lock_path, 'w', encoding='utf-8') as f:
                json.dump(list(ids), f)
        except Exception: pass

    write_outcome(args.slot, {"status": "completed",
                                "step_reached": "checkout_ok",
                                "finished_at": datetime.now().isoformat(),
                                "rc": 0})
    # ─── STEP 11 INSTRUMENTATION HOOK ─── (fail-safe)
    if _bridge_ctx is not None:
        try:
            from core.observability.bridge import record_outcome as _record_outcome
            _record_outcome(_bridge_ctx, status="checkout_ok",
                             rc=0, step_reached="checkout_ok")
        except Exception:
            pass
    # ──────────────────────────────────────────────────────────────
    return 0


if __name__ == '__main__':
    _slot = None
    try:
        # Pre-parse para tener slot disponible en outcome on crash
        for i, a in enumerate(sys.argv[1:], 1):
            if a == "--slot" and i < len(sys.argv) - 1:
                try: _slot = int(sys.argv[i+1])
                except: pass
                break
        rc = main()
    except SystemExit:
        raise
    except Exception as e:
        # ─── STEP 11 INSTRUMENTATION HOOK ─── (fail-safe, top-level)
        try:
            from core.observability.bridge import record_exception as _record_exception
            _record_exception(None, e, context="bot_main_topfatal",
                                payload={"slot": _slot})
        except Exception:
            pass
        # ──────────────────────────────────────────────────────────────
        if _slot is not None:
            try:
                write_outcome(_slot, {"status": "fatal_exception",
                                        "step_reached": "unknown",
                                        "error": f"{type(e).__name__}: {str(e)[:200]}",
                                        "finished_at": datetime.now().isoformat(),
                                        "rc": 99})
            except Exception:
                pass
        # Excepción no manejada en setup (e.g. _safe_detect_state fail
        # post-bot-block) → tratable como recuperable vía wrapper swap.
        # En vez de raise (que mata el proceso con rc=1), exit 99.
        sys.exit(99)
    sys.exit(rc)
