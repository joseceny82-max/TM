"""human_motion — utilidades de movimiento humano realista para Playwright.

Genera trayectorias de ratón Bezier cúbicas con jitter gaussiano y dwell times
gobernados por la cadena Markov de `behavior_chain`. Anti-detection contra:
  - NuData Security (mouse trajectory analysis)
  - Datadome intent-based detection
  - Akamai behavioral signals

API pública:
  - bezier_path(start, end, steps=30, curvature=0.3) -> List[(x,y)]
  - human_move(page, x, y, steps=None, jitter=True)
  - human_click(page, x, y, button='left')
  - human_type(page, selector, text, dwell_ms_avg=85)
  - move_to_element(page, selector)

Referencias:
  - Paper IJIRT-183343 "Emulating Human-Like Mouse Movement" (Bezier + Fitts)
  - https://blog.castle.io/from-puppeteer-stealth-to-nodriver/
"""
from __future__ import annotations

import math
import random
import time
from typing import List, Tuple, Optional, Any

Point = Tuple[float, float]


def _gauss_jitter(amount: float = 1.5) -> Tuple[float, float]:
    """Pequeño jitter gaussiano (μ=0, σ=amount) en x,y."""
    return random.gauss(0, amount), random.gauss(0, amount)


def bezier_path(start: Point, end: Point, steps: int = 30,
                curvature: float = 0.3) -> List[Point]:
    """Genera puntos de una curva Bezier cúbica entre `start` y `end`.

    `curvature` entre 0..1 controla cuánto se desvía la curva del segmento recto.
    Retorna lista de `steps+1` puntos (x,y) con jitter mínimo.
    """
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy) or 1.0
    # 2 puntos de control perpendiculares al segmento, escalados por curvature
    nx, ny = -dy / dist, dx / dist  # normal perpendicular
    offset = curvature * dist * random.uniform(0.4, 1.0) * random.choice((-1, 1))
    cp1 = (sx + dx * 0.3 + nx * offset, sy + dy * 0.3 + ny * offset)
    cp2 = (sx + dx * 0.7 + nx * offset * 0.5, sy + dy * 0.7 + ny * offset * 0.5)

    pts: List[Point] = []
    for i in range(steps + 1):
        t = i / steps
        # Cubic Bezier: B(t) = (1-t)³P0 + 3(1-t)²tP1 + 3(1-t)t²P2 + t³P3
        b = (1 - t)
        x = (b ** 3) * sx + 3 * (b ** 2) * t * cp1[0] + 3 * b * (t ** 2) * cp2[0] + (t ** 3) * ex
        y = (b ** 3) * sy + 3 * (b ** 2) * t * cp1[1] + 3 * b * (t ** 2) * cp2[1] + (t ** 3) * ey
        jx, jy = _gauss_jitter(amount=0.6)
        pts.append((x + jx, y + jy))
    return pts


def _fitts_steps(dist: float) -> int:
    """Fitts's Law: tiempo movimiento ≈ a + b·log2(D/W + 1).
    Usamos esto para derivar nº de steps razonable. Más distancia → más steps.
    """
    base = 8
    return int(base + math.log2(max(dist, 4) / 4 + 1) * 8 + random.randint(-2, 4))


def human_move(page: Any, x: float, y: float, steps: Optional[int] = None,
               jitter: bool = True, _start: Optional[Point] = None) -> dict:
    """Mueve el ratón siguiendo Bezier desde la posición previa hasta (x,y).

    Si `_start` no se pasa, asume (x,y)=(0,0) inicial — Playwright no expone la
    posición actual del cursor. Para mejor realismo, llama tras un click previo
    o tras `move_to_element` que registra `page._human_last_pos`.
    """
    last = getattr(page, "_human_last_pos", None) or _start or (0.0, 0.0)
    dist = math.hypot(x - last[0], y - last[1])
    n_steps = steps if steps is not None else _fitts_steps(dist)
    pts = bezier_path(last, (x, y), steps=n_steps, curvature=random.uniform(0.15, 0.4))
    try:
        for px, py in pts:
            page.mouse.move(px, py)
        # Última pausa antes de "asentar" el cursor (humano deja microcorreción)
        time.sleep(random.uniform(0.020, 0.080))
        try:
            setattr(page, "_human_last_pos", (x, y))
        except Exception:
            pass
        return {"ok": True, "steps": n_steps, "dist": round(dist, 1)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def human_click(page: Any, x: float, y: float, button: str = "left",
                dwell_ms: Optional[int] = None) -> dict:
    """Mueve el ratón con Bezier hasta (x,y) + click con dwell humano.

    Pre-dwell: Markov (behavior_chain) o gauss(180-280ms).
    Post-click micro-pausa: gauss(80-160ms).
    """
    pre_ms = dwell_ms
    if pre_ms is None:
        try:
            import behavior_chain
            pre_ms = int(behavior_chain.dwell_time("click") * 1000)
        except Exception:
            pre_ms = int(random.gauss(220, 60))
    pre_ms = max(60, min(pre_ms, 800))

    move_r = human_move(page, x, y)
    time.sleep(pre_ms / 1000.0)
    try:
        page.mouse.down(button=button)
        time.sleep(random.uniform(0.040, 0.120))
        page.mouse.up(button=button)
        time.sleep(random.uniform(0.080, 0.160))
        return {"ok": True, "pre_dwell_ms": pre_ms, **move_r}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", **move_r}


def human_type(page: Any, selector: str, text: str,
               dwell_ms_avg: int = 85, std: int = 30) -> dict:
    """Tipea `text` carácter a carácter con keydown→keyup flight time gauss.

    NuData mide `flight time` (intervalo keydown a keyup) y ritmo entre teclas.
    """
    try:
        page.locator(selector).first.click(timeout=5000)
    except Exception as e:
        return {"ok": False, "error": f"focus_failed:{e}"}

    typed = 0
    try:
        for ch in text:
            flight = max(35, int(random.gauss(dwell_ms_avg * 0.45, std * 0.5)))
            page.keyboard.press(ch, delay=flight)
            inter = max(40, int(random.gauss(dwell_ms_avg, std)))
            time.sleep(inter / 1000.0)
            typed += 1
        return {"ok": True, "chars": typed}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "chars": typed}


def move_to_element(page: Any, selector: str, offset_random: bool = True) -> dict:
    """Mueve el cursor al centro (con offset aleatorio) del elemento `selector`."""
    try:
        loc = page.locator(selector).first
        box = loc.bounding_box(timeout=5000)
        if not box:
            return {"ok": False, "error": "no_bbox"}
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        if offset_random:
            cx += random.uniform(-box["width"] * 0.15, box["width"] * 0.15)
            cy += random.uniform(-box["height"] * 0.15, box["height"] * 0.15)
        return human_move(page, cx, cy)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def click_element(page: Any, selector: str, button: str = "left") -> dict:
    """Move-to + click humano sobre selector."""
    try:
        loc = page.locator(selector).first
        box = loc.bounding_box(timeout=5000)
        if not box:
            return {"ok": False, "error": "no_bbox"}
        cx = box["x"] + box["width"] / 2 + random.uniform(-box["width"] * 0.2, box["width"] * 0.2)
        cy = box["y"] + box["height"] / 2 + random.uniform(-box["height"] * 0.2, box["height"] * 0.2)
        return human_click(page, cx, cy, button=button)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def self_test() -> dict:
    """Smoke test sin Playwright real — solo geometría + funciones puras."""
    out: dict = {"checks": {}}
    pts = bezier_path((0, 0), (500, 300), steps=20)
    out["checks"]["bezier_count"] = (len(pts) == 21)
    out["checks"]["bezier_endpoints"] = (
        abs(pts[0][0] - 0) < 5 and abs(pts[-1][0] - 500) < 5
        and abs(pts[0][1] - 0) < 5 and abs(pts[-1][1] - 300) < 5
    )
    out["checks"]["fitts_short"] = (_fitts_steps(50) >= 8)
    out["checks"]["fitts_long"] = (_fitts_steps(2000) >= 16)
    jx, jy = _gauss_jitter(2.0)
    out["checks"]["jitter_bounded"] = (abs(jx) < 20 and abs(jy) < 20)
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), indent=2))
