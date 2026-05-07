"""behavior_chain.py — Markov chain de navegación humana TM.

Modela la secuencia natural de un usuario real (`home → search → event → seatmap
→ cart → checkout`) con probabilidades de transición y dwell-times log-normales
por página. El bot puede usar este módulo para decidir el próximo `step` y el
tiempo a esperar, evitando el patrón delator de "saltar directo a ?id=X".

Características:
  - Transition matrix por defecto basada en patterns públicos de tickets/e-commerce.
  - Dwell-time log-normal por estado (μ y σ ajustables).
  - Exit patterns: probabilidad de abandono por estado.
  - `sample_path(target=...)` genera una ruta plausible que termine en target.
  - `next_step(state)` para decisiones online (un solo salto).
  - `learn_from_session(states, dwells)` actualiza la matriz con datos reales.

API pública:
  STATES (lista canónica)
  build_default_matrix() → dict transition matrix
  sample_path(target='checkout', max_steps=10, seed=None) → list of (state, dwell_s)
  next_step(state, *, matrix=None, seed=None) → str
  dwell_time(state, *, mu=None, sigma=None) → float seconds
  learn_from_session(states, dwells) → dict (cuántas transiciones contadas)
  load_matrix() / save_matrix(matrix)
  self_test() → dict
  get_info() → dict
"""
from __future__ import annotations

import json
import math
import random
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import config
import json_atomic

_MATRIX_KEY = "behavior_chain_matrix.json"
_LOG_KEY = "behavior_chain_log.json"
_lock = threading.RLock()

# ─── Estados canónicos ────────────────────────────────────────────
STATES = ["home", "search", "event", "seatmap", "cart",
          "checkout", "account", "exit"]
TERMINAL = {"checkout", "exit"}

# Dwell-time log-normal por estado: (mu, sigma) en segundos log-space
DWELL_PARAMS = {
    "home":     (1.6, 0.6),   # ~5s mediana
    "search":   (2.3, 0.5),   # ~10s
    "event":    (3.4, 0.7),   # ~30s
    "seatmap":  (3.7, 0.6),   # ~40s
    "cart":     (3.0, 0.5),   # ~20s
    "checkout": (4.0, 0.6),   # ~55s
    "account":  (3.0, 0.5),   # ~20s
    "exit":     (0.0, 0.0),
}


def build_default_matrix() -> Dict[str, Dict[str, float]]:
    """Devuelve transition matrix por defecto (filas suman 1.0)."""
    m = {
        "home":    {"search": 0.40, "event": 0.30, "account": 0.10,
                     "home": 0.10, "exit": 0.10},
        "search":  {"event": 0.55, "search": 0.20, "home": 0.10,
                     "account": 0.05, "exit": 0.10},
        "event":   {"seatmap": 0.45, "cart": 0.20, "search": 0.15,
                     "event": 0.05, "home": 0.05, "exit": 0.10},
        "seatmap": {"cart": 0.55, "event": 0.20, "seatmap": 0.10,
                     "search": 0.05, "exit": 0.10},
        "cart":    {"checkout": 0.55, "event": 0.10, "seatmap": 0.10,
                     "cart": 0.10, "exit": 0.15},
        "account": {"home": 0.40, "search": 0.20, "event": 0.20,
                     "account": 0.10, "exit": 0.10},
        "checkout":{"checkout": 1.0},  # estado absorbente (target)
        "exit":    {"exit": 1.0},
    }
    # validar suma 1.0 (tolerancia 1e-6)
    for s, row in m.items():
        total = sum(row.values())
        if abs(total - 1.0) > 1e-6:
            # normalizar
            for k in row:
                row[k] = row[k] / total
    return m


# ─── Persistencia ─────────────────────────────────────────────────
def load_matrix() -> Dict[str, Dict[str, float]]:
    with _lock:
        d = json_atomic.load(config.ruta(_MATRIX_KEY), default=None)
        if not isinstance(d, dict) or "matrix" not in d:
            m = build_default_matrix()
            json_atomic.save(config.ruta(_MATRIX_KEY),
                              {"matrix": m, "saved_at": datetime.now().isoformat()})
            return m
        return d["matrix"]


def save_matrix(matrix: Dict[str, Dict[str, float]]) -> Dict:
    """Guarda la matrix y devuelve {ok, saved_at}."""
    if not isinstance(matrix, dict):
        return {"ok": False, "error": "matrix must be dict"}
    # validar normalización
    for s, row in matrix.items():
        if not isinstance(row, dict) or not row:
            return {"ok": False, "error": f"row {s} empty"}
        total = sum(row.values())
        if total <= 0:
            return {"ok": False, "error": f"row {s} sums to {total}"}
        for k in row:
            row[k] = row[k] / total
    saved_at = datetime.now().isoformat()
    with _lock:
        json_atomic.save(config.ruta(_MATRIX_KEY),
                          {"matrix": matrix, "saved_at": saved_at})
    return {"ok": True, "saved_at": saved_at}


# ─── Transitions ──────────────────────────────────────────────────
def next_step(state: str, *, matrix: Optional[Dict] = None,
              seed: Optional[int] = None) -> str:
    """Sample del siguiente estado según matrix[state]."""
    rng = random.Random(seed) if seed is not None else random
    m = matrix or load_matrix()
    row = m.get(state) or m.get("home", {})
    if not row:
        return "exit"
    items = list(row.items())
    weights = [v for _, v in items]
    return rng.choices([k for k, _ in items], weights=weights, k=1)[0]


def dwell_time(state: str, *, mu: Optional[float] = None,
                sigma: Optional[float] = None,
                seed: Optional[int] = None) -> float:
    """Devuelve tiempo de permanencia (segundos) para un estado, log-normal."""
    rng = random.Random(seed) if seed is not None else random
    if mu is None or sigma is None:
        mu, sigma = DWELL_PARAMS.get(state, (2.5, 0.5))
    if mu <= 0:
        return 0.0
    # log-normal: exp(mu + sigma*Z)
    z = rng.gauss(0, 1)
    val = math.exp(mu + sigma * z)
    return round(max(0.5, min(val, 600.0)), 2)  # clamp [0.5, 600]


def sample_path(target: str = "checkout", start: str = "home",
                 max_steps: int = 10, seed: Optional[int] = None,
                 matrix: Optional[Dict] = None) -> List[Tuple[str, float]]:
    """Genera una secuencia (state, dwell_s) hasta llegar a target o exit/max_steps."""
    rng = random.Random(seed) if seed is not None else random
    m = matrix or load_matrix()
    path: List[Tuple[str, float]] = []
    cur = start if start in m else "home"
    for i in range(max_steps):
        d = dwell_time(cur, seed=rng.random() if seed is None else (seed + i))
        path.append((cur, d))
        if cur == target or cur == "exit":
            break
        nxt = next_step(cur, matrix=m,
                         seed=(seed + 1000 + i) if seed is not None else None)
        cur = nxt
    return path


def total_dwell(path: List[Tuple[str, float]]) -> float:
    return round(sum(d for _, d in path), 2)


# ─── Aprendizaje desde sesiones reales ────────────────────────────
def learn_from_session(states: List[str], dwells: Optional[List[float]] = None,
                        *, smoothing: float = 0.1) -> Dict:
    """Actualiza la matrix sumando transiciones observadas.

    Cada par consecutivo (s_i, s_{i+1}) cuenta como +1. Después se renormaliza
    cada fila. `smoothing` es el peso (0-1) que se da a la observación nueva
    sobre la matrix anterior — un EMA simple.
    """
    if not states or len(states) < 2:
        return {"ok": False, "error": "need at least 2 states"}
    states = [s for s in states if s in DWELL_PARAMS]
    if len(states) < 2:
        return {"ok": False, "error": "no valid states recognized"}

    transitions: Dict[str, Dict[str, int]] = {}
    for a, b in zip(states[:-1], states[1:]):
        transitions.setdefault(a, {}).setdefault(b, 0)
        transitions[a][b] += 1

    with _lock:
        m = load_matrix()
        for src, dests in transitions.items():
            total_obs = sum(dests.values())
            obs_pct = {k: v / total_obs for k, v in dests.items()}
            row = m.setdefault(src, {})
            # EMA: row = (1-α)*row + α*obs
            keys = set(row.keys()) | set(obs_pct.keys())
            for k in keys:
                row[k] = (1 - smoothing) * row.get(k, 0) + smoothing * obs_pct.get(k, 0)
            # normalizar
            tot = sum(row.values())
            if tot > 0:
                for k in row:
                    row[k] = row[k] / tot
        save_matrix(m)

    # log
    _log_event({
        "ts": datetime.now().isoformat(),
        "states": states,
        "transitions_observed": sum(sum(d.values()) for d in transitions.values()),
        "smoothing": smoothing,
    })

    return {"ok": True,
            "transitions_observed": sum(sum(d.values()) for d in transitions.values()),
            "states_unique": len(set(states))}


def _log_event(entry: Dict, max_keep: int = 200):
    with _lock:
        d = json_atomic.load(config.ruta(_LOG_KEY), default={"events": []})
        d.setdefault("events", []).append(entry)
        d["events"] = d["events"][-max_keep:]
        json_atomic.save(config.ruta(_LOG_KEY), d)


def get_log(limit: int = 30) -> Dict:
    d = json_atomic.load(config.ruta(_LOG_KEY), default={"events": []})
    return {"ok": True, "events": d.get("events", [])[-limit:][::-1]}


# ─── Self-test ────────────────────────────────────────────────────
def self_test() -> Dict:
    r = {"checks": {}}

    # 1. matrix válida
    m = build_default_matrix()
    r["checks"]["matrix_has_states"] = all(s in m for s in
        ("home", "search", "event", "seatmap", "cart", "checkout"))
    r["checks"]["matrix_rows_sum_one"] = all(
        abs(sum(m[s].values()) - 1.0) < 1e-6 for s in m)

    # 2. next_step determinista con seed
    a = next_step("home", matrix=m, seed=42)
    b = next_step("home", matrix=m, seed=42)
    r["checks"]["next_step_deterministic_with_seed"] = a == b
    r["checks"]["next_step_in_state_set"] = a in STATES

    # 3. dwell_time positivo y razonable
    d_home = dwell_time("home", seed=1)
    d_check = dwell_time("checkout", seed=1)
    r["checks"]["dwell_positive"] = d_home > 0 and d_check > 0
    r["checks"]["dwell_clamped"] = d_home <= 600

    # 4. sample_path llega a target
    path = sample_path(target="checkout", max_steps=20, seed=7)
    last = path[-1][0] if path else None
    r["checks"]["path_starts_home"] = path and path[0][0] == "home"
    r["checks"]["path_terminates"] = last in TERMINAL or len(path) == 20
    r["checks"]["path_total_dwell_positive"] = total_dwell(path) > 0

    # 5. learn_from_session actualiza matrix sin romperla
    snapshot_before = json.dumps(load_matrix(), sort_keys=True)
    res_learn = learn_from_session(
        ["home", "search", "event", "seatmap", "cart", "checkout"],
        smoothing=0.05,
    )
    r["checks"]["learn_ok"] = res_learn.get("ok") is True
    r["checks"]["learn_observed_transitions"] = res_learn.get("transitions_observed") == 5
    after = load_matrix()
    r["checks"]["learn_keeps_normalized"] = all(
        abs(sum(after[s].values()) - 1.0) < 1e-3 for s in after if after[s])

    # 6. save/load roundtrip
    custom = build_default_matrix()
    custom["home"]["exit"] = 0.50
    # renormalizar fila home
    tot = sum(custom["home"].values())
    for k in custom["home"]:
        custom["home"][k] = custom["home"][k] / tot
    save_matrix(custom)
    loaded = load_matrix()
    r["checks"]["save_load_roundtrip"] = abs(loaded["home"]["exit"] - custom["home"]["exit"]) < 1e-6

    # restore default
    save_matrix(build_default_matrix())

    r["total_checks"] = len(r["checks"])
    r["passed_checks"] = sum(1 for v in r["checks"].values() if v)
    r["ok"] = r["passed_checks"] == r["total_checks"]
    return r


def get_info() -> Dict:
    m = load_matrix()
    return {
        "module": "behavior_chain",
        "version": 1,
        "states": STATES,
        "dwell_params": DWELL_PARAMS,
        "matrix_rows": len(m),
        "log_events": len((get_log(1).get("events") or [])),
    }
