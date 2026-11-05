"""tests/test_idempotency_log.py — Test suite for STEP 2 (idempotency).

Cubre keys + WAL + recovery scan + atomicity.

Categorías:
    1. Key determinism            (6 tests)
    2. Key sensitivity             (6 tests)
    3. Key validation              (4 tests)
    4. Intent / outcome writes     (6 tests)
    5. Record states               (5 tests)
    6. Orphan scan                 (5 tests)
    7. Atomicity & failure modes   (4 tests)
    8. Edge cases                  (5 tests)
    9. Public API surface          (3 tests)

stdlib + pytest only. Aislado en tmp_path (no toca runs/ real).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.idempotency import (
    DEFAULT_BUCKET_GRANULARITY,
    INTENT_FILE,
    KEY_PREFIX,
    OUTCOME_FILE,
    IdempotencyError,
    IdempotencyRecord,
    IntentAlreadyExists,
    OutcomeAlreadyExists,
    is_committed,
    is_orphan,
    is_valid_key,
    list_keys,
    load_record,
    make_idempotency_key,
    scan_orphan_intents,
    write_intent,
    write_outcome,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
@pytest.fixture
def isolated_root(tmp_path):
    """Devuelve un path string de un directorio tmp aislado."""
    return str(tmp_path / "idem_log")


def _key(**overrides) -> str:
    defaults = {
        "event_id": "EVT-1",
        "seats": ["S1", "S2"],
        "account_id": "ACC-1",
        "timestamp_iso": "2026-05-14T10:00:00+00:00",
    }
    defaults.update(overrides)
    return make_idempotency_key(**defaults)


# ════════════════════════════════════════════════════════════════════
#  1. KEY DETERMINISM
# ════════════════════════════════════════════════════════════════════
class TestKeyDeterminism:

    def test_same_inputs_same_key(self):
        assert _key() == _key()

    def test_seat_order_irrelevant(self):
        a = make_idempotency_key(event_id="E", seats=["S1", "S2", "S3"],
                                   account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        b = make_idempotency_key(event_id="E", seats=["S3", "S2", "S1"],
                                   account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        assert a == b

    def test_seats_as_dict_canonicalized(self):
        seats_a = [{"seat_id": "S1"}, {"seat_id": "S2"}]
        seats_b = ["S1", "S2"]
        a = make_idempotency_key(event_id="E", seats=seats_a,
                                   account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        b = make_idempotency_key(event_id="E", seats=seats_b,
                                   account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        assert a == b

    def test_duplicate_seats_deduplicated(self):
        a = make_idempotency_key(event_id="E", seats=["S1", "S1", "S2"],
                                   account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        b = make_idempotency_key(event_id="E", seats=["S1", "S2"],
                                   account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        assert a == b

    def test_whitespace_stripped(self):
        a = make_idempotency_key(event_id="  E  ", seats=["S1"],
                                   account_id=" A ",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        b = make_idempotency_key(event_id="E", seats=["S1"],
                                   account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        assert a == b

    def test_same_day_different_minute_same_key(self):
        a = make_idempotency_key(event_id="E", seats=["S1"], account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        b = make_idempotency_key(event_id="E", seats=["S1"], account_id="A",
                                   timestamp_iso="2026-05-14T22:45:33+00:00")
        # Default bucket = "day" → mismo día → misma key
        assert a == b


# ════════════════════════════════════════════════════════════════════
#  2. KEY SENSITIVITY
# ════════════════════════════════════════════════════════════════════
class TestKeySensitivity:

    def test_different_event_id(self):
        assert _key(event_id="E1") != _key(event_id="E2")

    def test_different_account(self):
        assert _key(account_id="A1") != _key(account_id="A2")

    def test_different_seats(self):
        assert _key(seats=["S1"]) != _key(seats=["S2"])

    def test_different_day_bucket(self):
        a = _key(timestamp_iso="2026-05-14T10:00:00+00:00")
        b = _key(timestamp_iso="2026-05-15T10:00:00+00:00")
        assert a != b

    def test_hour_bucket_distinguishes_hours(self):
        a = make_idempotency_key(event_id="E", seats=["S"], account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00",
                                   bucket_granularity="hour")
        b = make_idempotency_key(event_id="E", seats=["S"], account_id="A",
                                   timestamp_iso="2026-05-14T11:00:00+00:00",
                                   bucket_granularity="hour")
        assert a != b

    def test_extra_field_differentiates(self):
        a = _key()
        b = make_idempotency_key(event_id="EVT-1", seats=["S1", "S2"],
                                   account_id="ACC-1",
                                   timestamp_iso="2026-05-14T10:00:00+00:00",
                                   extra="card-A")
        assert a != b


# ════════════════════════════════════════════════════════════════════
#  3. KEY VALIDATION
# ════════════════════════════════════════════════════════════════════
class TestKeyValidation:

    def test_valid_key_passes(self):
        k = _key()
        assert is_valid_key(k)

    def test_empty_string_invalid(self):
        assert is_valid_key("") is False

    def test_wrong_prefix_invalid(self):
        assert is_valid_key("xx-abc123") is False

    def test_non_string_invalid(self):
        assert is_valid_key(None) is False
        assert is_valid_key(12345) is False
        assert is_valid_key([]) is False


# ════════════════════════════════════════════════════════════════════
#  4. INTENT / OUTCOME WRITES
# ════════════════════════════════════════════════════════════════════
class TestWrites:

    def test_write_intent_creates_file(self, isolated_root):
        k = _key()
        p = write_intent(k, {"foo": "bar"}, root=isolated_root)
        assert Path(p).exists()

    def test_write_outcome_creates_file(self, isolated_root):
        k = _key()
        write_intent(k, {}, root=isolated_root)
        p = write_outcome(k, {"paid": True}, root=isolated_root)
        assert Path(p).exists()

    def test_intent_payload_stored(self, isolated_root):
        k = _key()
        write_intent(k, {"event_id": "E1", "n_seats": 2}, root=isolated_root)
        rec = load_record(k, root=isolated_root)
        assert rec.intent_payload["payload"] == {"event_id": "E1", "n_seats": 2}

    def test_outcome_payload_stored(self, isolated_root):
        k = _key()
        write_outcome(k, {"order_id": "ORD-X"}, root=isolated_root)
        rec = load_record(k, root=isolated_root)
        assert rec.outcome_payload["payload"] == {"order_id": "ORD-X"}

    def test_strict_intent_rejects_overwrite(self, isolated_root):
        k = _key()
        write_intent(k, {"v": 1}, root=isolated_root, strict=False)
        with pytest.raises(IntentAlreadyExists):
            write_intent(k, {"v": 2}, root=isolated_root, strict=True)

    def test_strict_outcome_rejects_overwrite(self, isolated_root):
        k = _key()
        write_outcome(k, {"v": 1}, root=isolated_root, strict=False)
        with pytest.raises(OutcomeAlreadyExists):
            write_outcome(k, {"v": 2}, root=isolated_root, strict=True)

    def test_non_strict_overwrites_silently(self, isolated_root):
        k = _key()
        write_intent(k, {"v": 1}, root=isolated_root)
        write_intent(k, {"v": 2}, root=isolated_root)  # no strict
        rec = load_record(k, root=isolated_root)
        assert rec.intent_payload["payload"] == {"v": 2}


# ════════════════════════════════════════════════════════════════════
#  5. RECORD STATES
# ════════════════════════════════════════════════════════════════════
class TestRecordStates:

    def test_unknown_when_nothing_written(self, isolated_root):
        rec = load_record(_key(), root=isolated_root)
        assert rec.is_unknown()
        assert not rec.is_orphan()
        assert not rec.is_committed()

    def test_orphan_after_intent_only(self, isolated_root):
        k = _key()
        write_intent(k, {}, root=isolated_root)
        rec = load_record(k, root=isolated_root)
        assert rec.is_orphan()
        assert not rec.is_committed()
        assert not rec.is_unknown()

    def test_committed_after_both(self, isolated_root):
        k = _key()
        write_intent(k, {}, root=isolated_root)
        write_outcome(k, {}, root=isolated_root)
        rec = load_record(k, root=isolated_root)
        assert rec.is_committed()
        assert not rec.is_orphan()

    def test_is_committed_helper(self, isolated_root):
        k = _key()
        assert is_committed(k, root=isolated_root) is False
        write_intent(k, {}, root=isolated_root)
        write_outcome(k, {}, root=isolated_root)
        assert is_committed(k, root=isolated_root) is True

    def test_is_orphan_helper(self, isolated_root):
        k = _key()
        assert is_orphan(k, root=isolated_root) is False
        write_intent(k, {}, root=isolated_root)
        assert is_orphan(k, root=isolated_root) is True
        write_outcome(k, {}, root=isolated_root)
        assert is_orphan(k, root=isolated_root) is False


# ════════════════════════════════════════════════════════════════════
#  6. ORPHAN SCAN
# ════════════════════════════════════════════════════════════════════
class TestOrphanScan:

    def test_empty_log_no_orphans(self, isolated_root):
        assert scan_orphan_intents(root=isolated_root) == []

    def test_committed_not_orphan(self, isolated_root):
        k = _key()
        write_intent(k, {}, root=isolated_root)
        write_outcome(k, {}, root=isolated_root)
        assert scan_orphan_intents(root=isolated_root) == []

    def test_single_orphan_detected(self, isolated_root):
        k = _key(event_id="E_ORPH")
        write_intent(k, {}, root=isolated_root)
        orphans = scan_orphan_intents(root=isolated_root)
        assert len(orphans) == 1
        assert orphans[0]["key"] == k

    def test_multiple_orphans_listed(self, isolated_root):
        keys = [_key(event_id=f"E{i}") for i in range(3)]
        for k in keys:
            write_intent(k, {}, root=isolated_root)
        orphans = scan_orphan_intents(root=isolated_root)
        assert {o["key"] for o in orphans} == set(keys)

    def test_older_than_filter(self, isolated_root):
        # Escribimos un intent ahora; scan con older_than_seconds=3600 → nada
        k = _key()
        write_intent(k, {}, root=isolated_root)
        orphans = scan_orphan_intents(root=isolated_root,
                                        older_than_seconds=3600.0)
        assert orphans == []
        # Sin filtro → 1
        orphans = scan_orphan_intents(root=isolated_root)
        assert len(orphans) == 1


# ════════════════════════════════════════════════════════════════════
#  7. ATOMICITY & FAILURE MODES
# ════════════════════════════════════════════════════════════════════
class TestAtomicity:

    def test_no_partial_files_after_write(self, isolated_root):
        """Tras un write, no debe quedar ningún archivo .tmp huérfano."""
        k = _key()
        write_intent(k, {"data": "x" * 1000}, root=isolated_root)
        kd = Path(isolated_root) / k
        tmps = list(kd.glob(".*.tmp"))
        assert tmps == []

    def test_intent_payload_is_valid_json(self, isolated_root):
        k = _key()
        write_intent(k, {"complex": {"nested": [1, 2, 3]}}, root=isolated_root)
        rec = load_record(k, root=isolated_root)
        # JSON roundtrip
        s = json.dumps(rec.intent_payload)
        loaded = json.loads(s)
        assert loaded["payload"]["complex"]["nested"] == [1, 2, 3]

    def test_write_with_non_dict_payload_raises(self, isolated_root):
        k = _key()
        with pytest.raises(IdempotencyError):
            write_intent(k, "not-a-dict", root=isolated_root)
        with pytest.raises(IdempotencyError):
            write_outcome(k, ["also-not-a-dict"], root=isolated_root)

    def test_empty_key_raises(self, isolated_root):
        with pytest.raises(IdempotencyError):
            write_intent("", {}, root=isolated_root)


# ════════════════════════════════════════════════════════════════════
#  8. EDGE CASES
# ════════════════════════════════════════════════════════════════════
class TestEdgeCases:

    def test_list_keys_empty_root(self, isolated_root):
        assert list_keys(root=isolated_root) == []

    def test_list_keys_after_writes(self, isolated_root):
        for i in range(3):
            write_intent(_key(event_id=f"E{i}"), {}, root=isolated_root)
        keys = list_keys(root=isolated_root)
        assert len(keys) == 3

    def test_malformed_intent_file_returns_none_payload(self, isolated_root):
        k = _key()
        kd = Path(isolated_root) / k
        kd.mkdir(parents=True, exist_ok=True)
        # Escribimos un intent.json corrupto manualmente
        (kd / INTENT_FILE).write_text("{ not valid json", encoding="utf-8")
        rec = load_record(k, root=isolated_root)
        # No debe crashear — payload es None pero intent_exists also False
        # (porque _safe_load_json filtra malformados)
        assert rec.intent_payload is None
        assert rec.intent_exists is False

    def test_seats_none_treated_as_empty(self, isolated_root):
        a = make_idempotency_key(event_id="E", seats=None, account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        b = make_idempotency_key(event_id="E", seats=[], account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00")
        assert a == b

    def test_bucket_none_ignores_timestamp(self):
        a = make_idempotency_key(event_id="E", seats=["S"], account_id="A",
                                   timestamp_iso="2026-05-14T10:00:00+00:00",
                                   bucket_granularity="none")
        b = make_idempotency_key(event_id="E", seats=["S"], account_id="A",
                                   timestamp_iso="2026-12-25T10:00:00+00:00",
                                   bucket_granularity="none")
        assert a == b


# ════════════════════════════════════════════════════════════════════
#  9. PUBLIC API SURFACE
# ════════════════════════════════════════════════════════════════════
class TestPublicAPI:

    def test_all_exports_present(self):
        import core.idempotency as ci
        expected = {
            "make_idempotency_key", "is_valid_key",
            "write_intent", "write_outcome", "load_record",
            "is_committed", "is_orphan", "list_keys",
            "scan_orphan_intents",
            "IdempotencyRecord", "IdempotencyError",
            "IntentAlreadyExists", "OutcomeAlreadyExists",
        }
        for name in expected:
            assert hasattr(ci, name), f"Missing public export: {name}"

    def test_record_is_frozen(self, isolated_root):
        k = _key()
        write_intent(k, {}, root=isolated_root)
        rec = load_record(k, root=isolated_root)
        with pytest.raises((AttributeError, Exception)):
            rec.key = "tampered"

    def test_default_root_is_under_runs(self):
        from core.idempotency import DEFAULT_ROOT
        assert "runs" in DEFAULT_ROOT
        assert "idempotency" in DEFAULT_ROOT
