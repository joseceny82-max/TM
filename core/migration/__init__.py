"""core/migration/ — Migration orchestration layer (STEP 9+).

Permite migración progresiva legacy → core flow:
    - flags (Step 9)
    - flow_router (Step 9)
    - shadow_orchestrator (Step 9)
    - divergence_engine (Step 9)
    - canary_controller (Step 10)
    - fallback_monitor (Step 10)
    - cutover_state (Step 10)
    - validation_gates (Step 10)

PRINCIPIO ARQUITECTÓNICO:
    Legacy SIEMPRE primary execution path.
    Core es opt-in, shadow-first, con fallback automático.
"""
from core.migration.canary_controller import CanaryController
from core.migration.cutover_router import (
    DEFAULT_CANARY_RATE,
    DEFAULT_FLOW_MODE,
    FLOW_MODE_AUTO,
    FLOW_MODE_FLOWCORE,
    FLOW_MODE_LEGACY,
    FLOW_MODE_SIMULATED,
    VALID_FLOW_MODES,
    decide_path,
    execute_purchase,
    get_canary_rate,
    get_flow_mode,
    get_kill_switch,
    is_simulation_mode,
    reset_state,
    set_canary_rate,
    set_flow_mode,
    set_kill_switch,
    snapshot,
)
from core.migration.shadow_runner import (
    ShadowResult,
    in_canary,
    run_with_shadow,
)
from core.migration.cutover_state import (
    CutoverConfig,
    CutoverState,
    IllegalCutoverTransition,
    config_for,
    emergency_rollback,
    is_legal_transition,
    legal_transitions,
    rollback_state,
    transition,
)
from core.migration.divergence_engine import compute_divergence
from core.migration.fallback_monitor import (
    FallbackResult,
    execute_with_fallback,
)
from core.migration.flags import (
    MigrationFlags,
    get_flags,
    reload_flags,
    set_flags,
)
from core.migration.flow_router import route_flow
from core.migration.shadow_orchestrator import execute_with_shadow
from core.migration.validation_gates import (
    GateConfig,
    GateResult,
    can_progress,
)

__all__ = [
    # Step 9
    "MigrationFlags",
    "get_flags",
    "reload_flags",
    "set_flags",
    "route_flow",
    "execute_with_shadow",
    "compute_divergence",
    # Step 10
    "CanaryController",
    "CutoverState",
    "CutoverConfig",
    "IllegalCutoverTransition",
    "config_for",
    "legal_transitions",
    "is_legal_transition",
    "transition",
    "rollback_state",
    "emergency_rollback",
    "FallbackResult",
    "execute_with_fallback",
    "GateConfig",
    "GateResult",
    "can_progress",
    # Step 12 — Shadow runner
    "ShadowResult",
    "run_with_shadow",
    "in_canary",
    # Step 13 — Cutover router
    "execute_purchase",
    "decide_path",
    "get_flow_mode",
    "set_flow_mode",
    "get_kill_switch",
    "set_kill_switch",
    "get_canary_rate",
    "set_canary_rate",
    "reset_state",
    "snapshot",
    "FLOW_MODE_LEGACY",
    "FLOW_MODE_FLOWCORE",
    "FLOW_MODE_AUTO",
    "FLOW_MODE_SIMULATED",
    "is_simulation_mode",
    "VALID_FLOW_MODES",
    "DEFAULT_FLOW_MODE",
    "DEFAULT_CANARY_RATE",
]
