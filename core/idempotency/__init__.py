"""core/idempotency/ — Idempotency primitives (STEP 2 del refactor).

Patrón Write-Ahead Intent Log para idempotencia real sin DB.

PROBLEMA QUE RESUELVE:
    El flow actual graba `outcome.json` AL FINAL de la operación.
    Si el proceso crashea entre el side-effect (submit payment) y el
    write del outcome → el sistema "olvida" lo que ya pasó → al
    reintentar, paga 2 veces o "pierde" la compra.

SOLUCIÓN:
    1) write_intent(key, payload)   ← ANTES del side-effect
    2) <ejecutar side-effect>
    3) write_outcome(key, payload)  ← DESPUÉS de confirmar

    En reintento/recovery:
        record = load_record(key)
        - is_committed → ya está hecho, devolver outcome
        - is_orphan    → empezó pero no confirmó, requiere recovery
        - is_unknown   → nunca se intentó, proceder normal

PROPIEDADES:
    - Escritura atómica (os.replace después de tempfile)
    - 1 directorio por key → fácil scan/cleanup
    - stdlib only — sin DB, sin servicios externos
    - Determinístico — keys reproducibles vía make_idempotency_key
    - Sin lock global — concurrency-safe a nivel de FS por POSIX rename

NO SE INTEGRA TODAVÍA con el flow productivo (`_compra_parallel.py`).
Eso es un sub-paso posterior del roadmap.

Public API:
    make_idempotency_key(event_id, seats, account_id, ...) -> str
    is_valid_key(key) -> bool
    write_intent(key, payload, *, strict=False) -> path
    write_outcome(key, payload, *, strict=False) -> path
    load_record(key) -> IdempotencyRecord
    is_committed(key) -> bool
    is_orphan(key) -> bool
    list_keys() -> list[str]
    scan_orphan_intents(*, older_than_seconds) -> list[dict]

Excepciones:
    IdempotencyError, IntentAlreadyExists, OutcomeAlreadyExists
"""
from core.idempotency.keys import (
    DEFAULT_BUCKET_GRANULARITY,
    KEY_LENGTH,
    KEY_PREFIX,
    is_valid_key,
    make_idempotency_key,
)
from core.idempotency.log import (
    DEFAULT_ROOT,
    INTENT_FILE,
    OUTCOME_FILE,
    IdempotencyError,
    IdempotencyRecord,
    IntentAlreadyExists,
    OutcomeAlreadyExists,
    is_committed,
    is_orphan,
    list_keys,
    load_record,
    scan_orphan_intents,
    write_intent,
    write_outcome,
)

__all__ = [
    # Keys
    "make_idempotency_key",
    "is_valid_key",
    # Log primitives
    "write_intent",
    "write_outcome",
    "load_record",
    "is_committed",
    "is_orphan",
    "list_keys",
    "scan_orphan_intents",
    # Model
    "IdempotencyRecord",
    # Exceptions
    "IdempotencyError",
    "IntentAlreadyExists",
    "OutcomeAlreadyExists",
    # Constants
    "DEFAULT_BUCKET_GRANULARITY",
    "DEFAULT_ROOT",
    "INTENT_FILE",
    "OUTCOME_FILE",
    "KEY_PREFIX",
    "KEY_LENGTH",
]
