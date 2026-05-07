"""queue_handler.py — Detección y espera en Queue-it (sala de espera virtual TM).

TM usa Queue-it como sistema de cola. Cuando hay alta demanda, te redirige a
queue-it.net/?c=ticketmaster&... y muestra "Tu posición en la cola es N. Tiempo
estimado X minutos". Hay que esperar hasta que devuelva el control.

API:
    is_in_queue(page) -> bool
    get_queue_status(page) -> dict {position, estimated_minutes, message}
    wait_for_turn(page, max_minutes=120, poll_interval_s=10, on_progress=None) -> dict
"""
from __future__ import annotations
import re
import time
from typing import Callable, Dict, Optional, Any


_QUEUE_URL_HOSTS = ('queue-it.net', 'queue-it.com', 'queue-fair.net')


def is_in_queue(page) -> bool:
    """Detecta si la página actual está en una cola de Queue-it (URL o body)."""
    try:
        url = (page.url or '').lower()
        if any(h in url for h in _QUEUE_URL_HOSTS):
            return True
        body = page.evaluate("() => (document.body.innerText||'').slice(0,1500)") or ''
        bl = body.lower()
        if 'queue-it' in bl or 'sala de espera' in bl or 'tu posición en la cola' in bl \
                or 'esperando turno' in bl or 'queue position' in bl \
                or 'aproximadamente' in bl and 'minuto' in bl:
            return True
    except Exception:
        pass
    return False


_RE_POS_ES = re.compile(r'(?:posici[oó]n|n[uú]mero)\s*[:\-]?\s*(\d{1,7})', re.I)
_RE_POS_EN = re.compile(r'(?:position|number)\s*[:\-]?\s*(\d{1,7})', re.I)
_RE_TIME_ES = re.compile(r'(\d{1,4})\s*(?:minutos?|min\.?)', re.I)
_RE_TIME_EN = re.compile(r'(\d{1,4})\s*(?:minutes?|mins?)', re.I)


def get_queue_status(page) -> Dict[str, Any]:
    """Extrae posición y tiempo estimado del DOM. Devuelve dict con campos:
    {in_queue, position?, estimated_minutes?, message, url}"""
    out: Dict[str, Any] = {'in_queue': is_in_queue(page), 'url': page.url}
    if not out['in_queue']:
        return out
    try:
        body = page.evaluate("() => (document.body.innerText||'').slice(0,3000)") or ''
    except Exception:
        body = ''
    out['message'] = body[:400]

    for rx in (_RE_POS_ES, _RE_POS_EN):
        m = rx.search(body)
        if m:
            try: out['position'] = int(m.group(1))
            except Exception: pass
            break
    for rx in (_RE_TIME_ES, _RE_TIME_EN):
        m = rx.search(body)
        if m:
            try: out['estimated_minutes'] = int(m.group(1))
            except Exception: pass
            break

    # Detectar progreso bar si hay
    try:
        progress = page.evaluate(r"""() => {
            const bars = Array.from(document.querySelectorAll('progress, [role="progressbar"], [class*="progress" i]'));
            for (const b of bars) {
                const v = b.getAttribute('value') || b.getAttribute('aria-valuenow');
                if (v) return parseFloat(v);
            }
            return null;
        }""")
        if progress is not None:
            out['progress_pct'] = progress
    except Exception:
        pass

    return out


def wait_for_turn(page, max_minutes: int = 120, poll_interval_s: int = 10,
                  on_progress: Optional[Callable[[Dict[str, Any]], None]] = None
                  ) -> Dict[str, Any]:
    """Espera hasta que la página salga de la cola. Devuelve {ok, exited_after_s, last_status}.

    Args:
        max_minutes: timeout duro
        poll_interval_s: frecuencia de poll
        on_progress: callback(status) con el último status cada poll
    """
    start = time.time()
    deadline = start + (max_minutes * 60)
    last_status: Dict[str, Any] = {'in_queue': True}

    while time.time() < deadline:
        st = get_queue_status(page)
        last_status = st
        if on_progress:
            try: on_progress(st)
            except Exception: pass
        if not st.get('in_queue'):
            return {
                'ok': True,
                'exited_after_s': round(time.time() - start, 1),
                'last_status': st,
            }
        # Sigue en cola — wait
        page.wait_for_timeout(poll_interval_s * 1000)

    return {
        'ok': False,
        'error': f'queue_timeout_{max_minutes}min',
        'exited_after_s': round(time.time() - start, 1),
        'last_status': last_status,
    }
