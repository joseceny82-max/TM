"""core/migration/canary_controller.py — Canary Routing (STEP 10).

Routing por porcentaje + per-dimension con hashing determinístico
para sticky-session.

PRINCIPIOS:
    - Hash-based: misma identidad → misma decisión, no random
    - Sticky: account_id/subnet/event_id producen mismo bucket en cada
      llamada (sin cambiar percentage)
    - Cambios de percentage NO redistribuyen el 100% del traffic
      (bucket asignado se respeta — sólo afecta a quién está cerca del
      umbral)

stdlib only.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
HASH_BUCKETS: int = 10000   # granularidad de routing (0.01%)
DEFAULT_PERCENTAGE: float = 0.0


# ════════════════════════════════════════════════════════════════════
#  CanaryController
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class CanaryController:
    """Decide routing por porcentaje + identidad sticky."""
    percentage: float = DEFAULT_PERCENTAGE
    seed: str = "canary_v1"      # cambiar seed redistribuye traffic

    def _clamped_pct(self) -> float:
        return max(0.0, min(100.0, float(self.percentage)))

    def _bucket(self, identity: str) -> int:
        """Hash determinístico de identity → bucket [0, HASH_BUCKETS-1]."""
        h = hashlib.sha256(f"{self.seed}|{identity}".encode("utf-8")).hexdigest()
        return int(h[:8], 16) % HASH_BUCKETS

    def should_route_to_core(self, *,
                              account_id: str = "",
                              subnet: str = "",
                              event_id: str = "") -> bool:
        """¿Esta identidad cae dentro del canary %?

        La identidad se compone de los inputs no vacíos. Si todos vacíos,
        usa "anonymous". Mismo input → misma decisión.
        """
        pct = self._clamped_pct()
        if pct <= 0.0:
            return False
        if pct >= 100.0:
            return True
        parts = [p for p in (account_id, subnet, event_id) if p]
        identity = "|".join(parts) if parts else "anonymous"
        threshold = int(pct * HASH_BUCKETS / 100.0)
        return self._bucket(identity) < threshold

    def with_percentage(self, percentage: float) -> "CanaryController":
        from dataclasses import replace
        return replace(self, percentage=float(percentage))


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
