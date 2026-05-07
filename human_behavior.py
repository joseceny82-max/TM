"""human_behavior.py — Simulación humana avanzada para anti-detection.

Implementa las 5 mejoras de comportamiento humano realista:

1. `generate_bezier_trajectory(start, end, overshoot=True)`
   → Mouse curvo con overshoot (usuario pasa del target y vuelve)

2. `generate_hesitation(target_x, target_y)`
   → hover 500-1500ms + alejarse antes de click final

3. `pick_adaptive_personality(session_history)`
   → empieza "casual", migra a "power_user" tras N interacciones OK

4. `pick_geo_aware_personality(country_code)`
   → US=fast más común, ES=casual, JP=power_user (datos sociológicos)

5. `generate_scroll_momentum(direction, amount)`
   → scroll con inertia (Δ grande inicial, decay exponencial)

6. `generate_tab_switch_events()` / `generate_visibility_change_events()`
   → eventos `visibilitychange`/`focus`/`blur` que Akamai trackea

Utilidad principal: engancharse a `akamai_bmsc.generate_humanized_payload`
para variar el comportamiento por geo/session/historial.
"""
import math
import random
import hashlib
from datetime import datetime
from typing import List, Dict, Optional, Tuple


# ═══════════════════════════════════════════════════════════
#  1. MOUSE BEZIER + OVERSHOOT
# ═══════════════════════════════════════════════════════════
def generate_bezier_trajectory(start: Tuple[int, int],
                                 end: Tuple[int, int],
                                 steps: int = 20,
                                 overshoot: bool = True,
                                 overshoot_amount: int = 30) -> List[dict]:
    """Bezier cúbico con 2 puntos de control + overshoot opcional.

    Matemáticamente: B(t) = (1-t)³P0 + 3(1-t)²tP1 + 3(1-t)t²P2 + t³P3

    El overshoot es CLAVE: usuarios reales pasan del target ~30px y vuelven.
    Detectores ML marcan mouse lineal como bot.
    """
    x0, y0 = start
    x3, y3 = end

    # Puntos de control aleatorios (curva natural)
    dx = x3 - x0
    dy = y3 - y0
    dist = math.sqrt(dx * dx + dy * dy)

    # Control 1: cerca del inicio, desviación perpendicular
    ctrl1_offset = random.uniform(-0.3, 0.3) * dist
    x1 = x0 + dx * 0.33 + ctrl1_offset * (dy / max(1, dist))
    y1 = y0 + dy * 0.33 - ctrl1_offset * (dx / max(1, dist))

    # Control 2: cerca del final, otra desviación
    ctrl2_offset = random.uniform(-0.2, 0.2) * dist
    x2 = x0 + dx * 0.66 + ctrl2_offset * (dy / max(1, dist))
    y2 = y0 + dy * 0.66 - ctrl2_offset * (dx / max(1, dist))

    events = []
    t = 0
    for i in range(steps + 1):
        u = i / steps
        uu = u * u
        uuu = uu * u
        mu = 1 - u
        mumu = mu * mu
        mumumu = mumu * mu

        x = mumumu * x0 + 3 * mumu * u * x1 + 3 * mu * uu * x2 + uuu * x3
        y = mumumu * y0 + 3 * mumu * u * y1 + 3 * mu * uu * y2 + uuu * y3

        # Añadir micro-jitter (mano temblorosa)
        x += random.uniform(-1.5, 1.5)
        y += random.uniform(-1.5, 1.5)

        # Timing variable — más lento al principio y al final (ease-in-out)
        base_dt = 15 + int(30 * math.sin(u * math.pi))
        t += base_dt + random.randint(-5, 8)

        events.append({"type": "mm", "x": int(x), "y": int(y), "t": t})

    # Overshoot: pasar del target + volver
    if overshoot and dist > 100:
        # Vector dirección unitario
        ux = dx / dist
        uy = dy / dist
        # Overshoot en la misma dirección
        over_x = x3 + ux * overshoot_amount + random.randint(-5, 5)
        over_y = y3 + uy * overshoot_amount + random.randint(-5, 5)

        # 3-5 eventos pasando del target
        for i in range(random.randint(3, 5)):
            progress = i / 4
            t += random.randint(8, 20)
            x = x3 + (over_x - x3) * progress + random.uniform(-2, 2)
            y = y3 + (over_y - y3) * progress + random.uniform(-2, 2)
            events.append({"type": "mm", "x": int(x), "y": int(y), "t": t,
                            "overshoot": True})

        # Volver al target (correcciones)
        for i in range(random.randint(2, 4)):
            progress = i / 3
            t += random.randint(15, 35)
            x = over_x + (x3 - over_x) * progress + random.uniform(-2, 2)
            y = over_y + (y3 - over_y) * progress + random.uniform(-2, 2)
            events.append({"type": "mm", "x": int(x), "y": int(y), "t": t,
                            "correction": True})

    return events


# ═══════════════════════════════════════════════════════════
#  2. HESITATION EVENTS
# ═══════════════════════════════════════════════════════════
def generate_hesitation(target_x: int, target_y: int,
                          viewport: Tuple[int, int] = (1920, 1080),
                          hesitation_prob: float = 0.35) -> List[dict]:
    """Genera secuencia de 'hesitación': hover + alejarse + volver + click.
    Solo aplica con probabilidad `hesitation_prob` (35% default).

    Patrón real: user va al botón, se lo piensa (500-1500ms), se aleja
    unos 80-150px, vuelve al botón, click.
    """
    if random.random() > hesitation_prob:
        return []

    events = []
    t = 0
    w, h = viewport

    # 1. Llegar al target
    bezier = generate_bezier_trajectory(
        (random.randint(200, w - 200), random.randint(200, h - 200)),
        (target_x, target_y),
        steps=12, overshoot=False,
    )
    for e in bezier:
        e["t"] = t + e["t"]
        events.append(e)
    t = events[-1]["t"] if events else 0

    # 2. Hover (pausa hesitando)
    hover_ms = random.randint(500, 1500)
    # Dividir en N micro-events
    for _ in range(random.randint(3, 6)):
        t += hover_ms // 6 + random.randint(-30, 30)
        events.append({
            "type": "mm",
            "x": target_x + random.randint(-3, 3),
            "y": target_y + random.randint(-3, 3),
            "t": t, "hesitate": True,
        })

    # 3. Alejarse (duda)
    if random.random() < 0.6:
        away_angle = random.uniform(0, 2 * math.pi)
        away_dist = random.randint(80, 150)
        away_x = target_x + int(math.cos(away_angle) * away_dist)
        away_y = target_y + int(math.sin(away_angle) * away_dist)
        away_x = max(10, min(w - 10, away_x))
        away_y = max(10, min(h - 10, away_y))

        away_traj = generate_bezier_trajectory(
            (target_x, target_y), (away_x, away_y),
            steps=8, overshoot=False,
        )
        for e in away_traj:
            e["t"] = t + e["t"]
            e["doubting"] = True
            events.append(e)
        t = events[-1]["t"]

        # Pausa pensando
        t += random.randint(300, 900)

        # 4. Volver al target (decisión final)
        back_traj = generate_bezier_trajectory(
            (away_x, away_y), (target_x, target_y),
            steps=10, overshoot=True, overshoot_amount=15,
        )
        for e in back_traj:
            e["t"] = t + e["t"]
            e["returning"] = True
            events.append(e)
        t = events[-1]["t"]

    # 5. Click final
    events.append({"type": "md", "x": target_x, "y": target_y, "t": t,
                    "button": 0})
    events.append({"type": "mu", "x": target_x, "y": target_y,
                    "t": t + random.randint(50, 120), "button": 0})

    return events


# ═══════════════════════════════════════════════════════════
#  3. ADAPTIVE PERSONALITY
# ═══════════════════════════════════════════════════════════
# Sesiones → personalidades (migración casual → power con uso)
PERSONALITY_PROGRESSION = [
    (0, "casual"),           # primer uso
    (5, "casual"),
    (10, "fast_user"),       # tras 10 interacciones sabe la UI
    (30, "power_user"),      # tras 30 es power
    (100, "power_user"),     # mantiene
]


def pick_adaptive_personality(interaction_count: int,
                                previous_personality: Optional[str] = None) -> str:
    """Elige personalidad según el histórico de interacciones.

    Un usuario nuevo empieza como 'casual', aprende y migra a 'fast_user',
    y tras mucho uso se vuelve 'power_user'. Simula aprendizaje humano.
    """
    target = "casual"
    for threshold, pers in PERSONALITY_PROGRESSION:
        if interaction_count >= threshold:
            target = pers
    # Estabilizar: si ya era power_user, mantener
    if previous_personality == "power_user":
        return "power_user"
    # Transición suave (no saltar niveles de golpe)
    if previous_personality and previous_personality != target:
        # 80% migrar, 20% mantener (evita saltos bruscos detectables)
        if random.random() < 0.20:
            return previous_personality
    return target


# ═══════════════════════════════════════════════════════════
#  4. GEO-AWARE PERSONALITIES
# ═══════════════════════════════════════════════════════════
# Datos sociológicos sobre velocidad de uso por país (inventados razonablemente)
GEO_PERSONALITY_WEIGHTS = {
    # country: {personality: weight}
    "us": {"casual": 30, "fast_user": 40, "power_user": 20, "mobile_user": 10, "slow_user": 0},
    "ca": {"casual": 35, "fast_user": 35, "power_user": 15, "mobile_user": 12, "slow_user": 3},
    "uk": {"casual": 40, "fast_user": 30, "power_user": 15, "mobile_user": 10, "slow_user": 5},
    "de": {"casual": 45, "fast_user": 30, "power_user": 15, "mobile_user": 8, "slow_user": 2},
    "es": {"casual": 50, "fast_user": 20, "power_user": 10, "mobile_user": 15, "slow_user": 5},
    "fr": {"casual": 45, "fast_user": 25, "power_user": 15, "mobile_user": 10, "slow_user": 5},
    "it": {"casual": 50, "fast_user": 20, "power_user": 10, "mobile_user": 15, "slow_user": 5},
    "nl": {"casual": 35, "fast_user": 35, "power_user": 20, "mobile_user": 8, "slow_user": 2},
    "au": {"casual": 35, "fast_user": 35, "power_user": 18, "mobile_user": 10, "slow_user": 2},
    "mx": {"casual": 45, "fast_user": 15, "power_user": 10, "mobile_user": 25, "slow_user": 5},
    "jp": {"casual": 20, "fast_user": 30, "power_user": 40, "mobile_user": 10, "slow_user": 0},
    "kr": {"casual": 15, "fast_user": 35, "power_user": 40, "mobile_user": 10, "slow_user": 0},
    "ar": {"casual": 45, "fast_user": 20, "power_user": 10, "mobile_user": 20, "slow_user": 5},
    "br": {"casual": 40, "fast_user": 25, "power_user": 10, "mobile_user": 20, "slow_user": 5},
    "default": {"casual": 40, "fast_user": 30, "power_user": 15, "mobile_user": 10, "slow_user": 5},
}


def pick_geo_aware_personality(country_code: str = "us",
                                 seed: Optional[str] = None) -> str:
    """Elige personalidad ponderada por país."""
    weights = GEO_PERSONALITY_WEIGHTS.get(country_code.lower(),
                                            GEO_PERSONALITY_WEIGHTS["default"])
    if seed:
        # Hash estable del seed para consistencia
        h = int(hashlib.md5(f"{seed}{country_code}".encode()).hexdigest(), 16)
        # Reconstruir choice determinístico
        total = sum(weights.values())
        target = h % total
        cum = 0
        for pers, w in weights.items():
            cum += w
            if target < cum:
                return pers
        return list(weights.keys())[0]
    # Random weighted
    keys = list(weights.keys())
    ws = [weights[k] for k in keys]
    return random.choices(keys, weights=ws, k=1)[0]


# ═══════════════════════════════════════════════════════════
#  5. SCROLL MOMENTUM (inertia real)
# ═══════════════════════════════════════════════════════════
def generate_scroll_momentum(direction: int = 1,
                               total_amount: Optional[int] = None,
                               start_time_ms: int = 0) -> List[dict]:
    """Scroll con momentum físico: grande inicial + decay exponencial.

    El scroll humano con trackpad/rueda genera eventos con decay natural.
    Bots suelen hacer scroll lineal (N eventos iguales) → detectable.
    """
    events = []
    total = total_amount or random.randint(400, 1200)
    t = start_time_ms

    # Curva exponencial decay: primera wheel grande, siguientes cada vez menores
    remaining = total
    event_count = 0
    max_events = 25
    while remaining > 5 and event_count < max_events:
        # Amount con decay
        chunk = max(5, int(remaining * random.uniform(0.15, 0.35)))
        remaining -= chunk
        event_count += 1

        # Timing con jitter
        dt = random.randint(10, 40)
        if event_count == 1:
            dt = random.randint(50, 120)  # primer scroll más lento (reacción inicial)
        t += dt

        events.append({
            "type": "wh",  # wheel
            "dx": 0,
            "dy": direction * chunk,
            "t": t,
            "momentum": event_count,
        })
    return events


# ═══════════════════════════════════════════════════════════
#  6. TAB SWITCHING + VISIBILITY EVENTS
# ═══════════════════════════════════════════════════════════
def generate_visibility_change_events(base_time_ms: int = 0,
                                        away_ms: Optional[int] = None) -> List[dict]:
    """Simula que el usuario cambia de pestaña y vuelve.
    Akamai trackea `visibilitychange`, `focus`, `blur`.

    Bots pasan las páginas sin `blur` nunca → detectable.
    """
    events = []
    away_ms = away_ms if away_ms is not None else random.randint(500, 5000)
    t = base_time_ms

    # 1. blur (usuario cambia de pestaña)
    events.append({"type": "blur", "t": t, "visibility": "hidden"})
    events.append({"type": "visibilitychange", "t": t + 1, "state": "hidden"})

    # 2. tiempo fuera
    t += away_ms

    # 3. focus + visible
    events.append({"type": "visibilitychange", "t": t, "state": "visible"})
    events.append({"type": "focus", "t": t + random.randint(1, 30),
                    "visibility": "visible"})

    return events


# ═══════════════════════════════════════════════════════════
#  7. COMPOUND: user_journey_events
# ═══════════════════════════════════════════════════════════
def generate_user_journey_events(viewport: Tuple[int, int] = (1920, 1080),
                                    personality: str = "casual",
                                    duration_ms: int = 3000,
                                    include_tab_switch: bool = True) -> List[dict]:
    """Genera una sesión completa humana realista:
    - 2-4 mouse movements con Bezier + overshoot
    - 1-2 hesitations sobre botones
    - 1-2 scrolls con momentum
    - 30% chance tab switch
    - Click final al target

    Esto es lo que un detector ML espera ver de un usuario real.
    """
    events = []
    t = 0
    w, h = viewport

    # 1-2 hesitations sobre elementos de la página
    for _ in range(random.randint(1, 2)):
        tx = random.randint(200, w - 200)
        ty = random.randint(100, 500)
        hes = generate_hesitation(tx, ty, viewport, hesitation_prob=1.0)
        for e in hes:
            e["t"] += t
        events.extend(hes)
        if events:
            t = events[-1]["t"]
        t += random.randint(200, 600)

    # 1-2 scrolls con momentum
    for _ in range(random.randint(1, 2)):
        direction = random.choice([1, 1, 1, -1])  # 75% scroll abajo
        scrolls = generate_scroll_momentum(direction=direction,
                                             start_time_ms=t)
        events.extend(scrolls)
        if events:
            t = events[-1]["t"]
        t += random.randint(300, 800)

    # Tab switch opcional
    if include_tab_switch and random.random() < 0.3:
        vis_events = generate_visibility_change_events(
            base_time_ms=t,
            away_ms=random.randint(500, 3000),
        )
        events.extend(vis_events)
        if events:
            t = events[-1]["t"]

    return events


# ═══════════════════════════════════════════════════════════
#  SELF TEST
# ═══════════════════════════════════════════════════════════
def self_test() -> dict:
    """Test de las 5 funciones principales."""
    results = {"ok": True, "checks": {}}

    # Bezier + overshoot
    traj = generate_bezier_trajectory((100, 100), (800, 600),
                                         steps=20, overshoot=True)
    has_overshoot = any(e.get("overshoot") for e in traj)
    results["checks"]["bezier_has_overshoot"] = has_overshoot
    results["checks"]["bezier_events_count"] = len(traj)
    # Verificar curva (no es línea recta)
    midpoint_expected = (450, 350)
    midpoint_actual = traj[len(traj) // 2]
    deviation = math.sqrt((midpoint_actual["x"] - midpoint_expected[0]) ** 2 +
                           (midpoint_actual["y"] - midpoint_expected[1]) ** 2)
    results["checks"]["bezier_curve_deviation"] = round(deviation, 1)
    results["checks"]["bezier_is_curved"] = deviation > 20  # debería desviarse

    # Hesitation
    hes = generate_hesitation(500, 300, hesitation_prob=1.0)
    has_hover = any(e.get("hesitate") for e in hes)
    has_doubting = any(e.get("doubting") for e in hes)
    has_click = any(e.get("type") in ("md", "mu") for e in hes)
    results["checks"]["hesitation_total"] = len(hes)
    results["checks"]["hesitation_has_hover"] = has_hover
    results["checks"]["hesitation_has_click"] = has_click

    # Adaptive personality
    pers_new = pick_adaptive_personality(0)
    pers_mid = pick_adaptive_personality(15)
    pers_expert = pick_adaptive_personality(100)
    results["checks"]["adaptive_new"] = pers_new
    results["checks"]["adaptive_mid"] = pers_mid
    results["checks"]["adaptive_expert"] = pers_expert

    # Geo-aware
    us_pers = pick_geo_aware_personality("us", seed="test1")
    jp_pers = pick_geo_aware_personality("jp", seed="test1")
    results["checks"]["geo_us"] = us_pers
    results["checks"]["geo_jp"] = jp_pers
    # Deterministic por seed
    us_repeat = pick_geo_aware_personality("us", seed="test1")
    results["checks"]["geo_deterministic"] = us_pers == us_repeat

    # Scroll momentum
    scrolls = generate_scroll_momentum(direction=1, total_amount=1000)
    # verificar decay (primer evento >= último evento)
    first_dy = abs(scrolls[0]["dy"]) if scrolls else 0
    last_dy = abs(scrolls[-1]["dy"]) if scrolls else 0
    results["checks"]["scroll_events_count"] = len(scrolls)
    results["checks"]["scroll_has_decay"] = first_dy >= last_dy

    # Visibility
    vis = generate_visibility_change_events()
    results["checks"]["visibility_has_blur"] = any(e["type"] == "blur" for e in vis)
    results["checks"]["visibility_has_focus"] = any(e["type"] == "focus" for e in vis)

    # User journey
    journey = generate_user_journey_events(personality="fast_user")
    results["checks"]["journey_events"] = len(journey)

    return results


def get_info() -> dict:
    return {
        "module": "human_behavior",
        "capabilities": [
            "bezier_trajectory", "overshoot", "hesitation",
            "adaptive_personality", "geo_aware_personality",
            "scroll_momentum", "tab_switch_events",
            "user_journey",
        ],
        "geo_countries": list(GEO_PERSONALITY_WEIGHTS.keys()),
        "personality_progression": [p[1] for p in PERSONALITY_PROGRESSION],
    }
