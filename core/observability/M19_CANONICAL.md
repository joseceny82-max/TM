# M19 — Canonical Domain Adapter Contract

> **Status**: LOCKED · canonical · single source of truth
> **Date**: 2026-05-14
> **Scope**: Domain boundary translation Market / Simulation → TM canonical

---

## 1. Statement of canonicity

There is **one and only one** M19 in the system:

```
core.observability.adapter.domain_adapter.adapt_event_to_tm_snapshot
```

**There are no M19A / M19B / v2 / forks.**

Any namespace that re-exports M19 (such as `core.observability.domain_adapter`)
is a **compatibility shim only**, never a parallel implementation.

---

## 2. Public surface (frozen)

```python
from core.observability.adapter import (
    adapt_event_to_tm_snapshot,             # primary name
    adapt_market_event,                      # semantic alias (Market domain)
    adapt_market_event_to_simulation,        # semantic alias (Simulation domain)
    DRIFT_HINT_THRESHOLDS,                   # bucketing constants
    VOLUME_ERROR_THRESHOLDS,
    MAX_ERROR_PROXY,
)
```

Equivalent (shim — identical objects):

```python
from core.observability.domain_adapter import (
    adapt_event_to_tm_snapshot,
    adapt_market_event_to_simulation,
)
```

All three names (`adapt_event_to_tm_snapshot`, `adapt_market_event`,
`adapt_market_event_to_simulation`) are **the same function object**.

Both imports **MUST** resolve to the same Python function object
(`is` comparison passes). Verified by `tests/test_m19_canonical.py`.

---

## 3. Input contract (multi-shape, single adapter)

The single canonical adapter accepts **two input shapes**:

### Shape A — MarketEvent (flat)
```python
{
    "event_id":         str,
    "timestamp":        str,
    "asset":            str,
    "signals":          {volatility, liquidity, volume, ...},
    "external_factors": {macro_risk, ...},
    "system_inputs":    {...},
    "context":          {...},
}
```

### Shape B — SimulationEvent (envelope)
```python
{
    "source":            "simulation" | "replay" | "live" | "market" | "synthetic",
    "event":             {<MarketEvent>},
    "determinism_seed":  int,
}
```

Detection heuristic (in `_unwrap_simulation_envelope`):
- `event["event"]` is dict AND `"source"` in `event` → envelope shape
- Otherwise → flat shape (default `mode = "market"`)

**There is no third shape.** New input domains MUST be wrapped in
SimulationEvent envelope with a registered `mode`.

---

## 4. Output contract (frozen schema)

```python
{
    "history": list[dict],          # NEVER "history_snapshot"
    "current": {
        "rates": {
            "purchase_dry_run_rate": float ∈ [0, 1],
            "purchase_block_rate":   float ∈ [0, 1],
        },
        "counters": {
            "purchase_live_count": int ≥ 0,
            "internal_errors":     int ∈ [0, 3],
        },
        "config": {"mode": "dry_run"},
    },
    "meta": {
        "mode":       "simulation" | "replay" | "live" | "market" | "synthetic",
        "event_id":   str,
        "timestamp":  str,
        "drift_hint": "low" | "medium" | "high" | "critical",
    },
}
```

**The schema is frozen.** Schema mirrors such as `history_snapshot`,
`current_snapshot`, `exposure_level`, `news_sentiment` referenced in
prior drafts are **NOT** part of the contract.

If a future need arises for enrichment fields, they must be added under
`meta.extensions` and documented in a versioned amendment to this file.

---

## 5. Mapping (fixed, deterministic, not calibratable)

| Input signal                | Output target                  | Function     |
|-----------------------------|--------------------------------|--------------|
| `signals.volatility`        | `rates.purchase_block_rate`    | identity     |
| `signals.liquidity`         | `rates.purchase_dry_run_rate`  | identity     |
| `external_factors.macro_risk` | `meta.drift_hint`            | 4-bucket     |
| `signals.volume`            | `counters.internal_errors`     | 4-bucket cap 3 |

All bucketing thresholds are constants in `domain_adapter.py`. Changing
them is a **breaking change** to M19 and invalidates M20 Fidelity / Shadow
baselines. Any change requires a versioned amendment.

---

## 6. Behavioral guarantees

| ID | Guarantee | Verified by |
|----|-----------|-------------|
| G1 | Market domain never reaches M13.7+ directly | architecture |
| G2 | Core (M13.7–M18) unmodified by M19 | architecture |
| G3 | M19 advises only — never decides | `tests/test_domain_adapter.py` |
| G4 | No feedback loop M13.7 → M19 | architecture |
| G5 | Pure function: no IO, no random, no clock | `tests/test_domain_adapter.py` (AST) |
| G6 | Deterministic: same input → same output | `tests/test_domain_adapter.py` |
| G7 | Fail-safe: malformed input → default snapshot | `tests/test_domain_adapter.py` |
| G8 | Identity across namespaces (canonical = shim) | `tests/test_m19_canonical.py` |
| G9 | No-divergence: shim has no logic to drift | code review + `tests/test_m19_canonical.py` |
| G10 | Schema invariance: only `{history, current, meta}` | `tests/test_m19_canonical.py` |

---

## 7. Dependents

These layers consume M19 as their **single source of truth**:

| Layer | Path | Consumption |
|-------|------|-------------|
| M13.7–M18 (decision core) | `core/observability/{drift, risk, readiness, autonomy, governance, policy, audit, explainability}/` | reads `TM_Snapshot` |
| M20 Fidelity | `core/observability/simulation_fidelity/` | evaluates adapter quality |
| M20 Shadow  | `core/observability/shadow/` | observes dynamics of `TM_Snapshot` stream |

If M19 forks, **all of the above lose their baseline**. This is why
canonicalization is enforced.

---

## 8. What this contract forbids

- Creating `core/observability/adapter_v2/` with parallel implementation
- Creating `core/observability/domain_adapter/domain_adapter.py` with own logic
- Adding `history_snapshot` / `current_snapshot` schema mirrors
- Adding `exposure_level` / `news_sentiment` enrichments at adapter level
- Splitting M19 into M19A / M19B
- Calibrating thresholds externally (env vars, config files, runtime params)
- Importing from M11–M18 inside the adapter (breaks epistemic independence)

---

## 9. Amendment procedure

Any change to:
- the output schema
- the input shapes accepted
- the mapping thresholds

requires:

1. A versioned section appended to this file (`## v2`, `## v3`, ...)
2. A migration test in `tests/test_m19_canonical.py` proving backward
   compatibility OR an explicit breaking-change marker
3. Re-baselining of M20 Fidelity expected values
4. Re-running the M20 Shadow scenario report

Until such an amendment is added, **M19 is frozen at v1 (2026-05-14)**.

---

**End of canonical contract.**
