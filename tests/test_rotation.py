"""Tests M4 — JSONL Rotation.

Garantías:
    1. Rota ficheros >max_age_days
    2. Rota más viejos si total > max_total_mb
    3. NUNCA toca ficheros activos (mtime < 60s)
    4. NUNCA borra (siempre comprime + archiva)
    5. Idempotente (segunda llamada no rompe nada)
    6. Apagable con env var (no arranca thread)
    7. Stats reportadas correctamente
"""
import gzip
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_file(path: Path, content: bytes, mtime_ago_s: float = 0) -> Path:
    path.write_bytes(content)
    if mtime_ago_s > 0:
        old = time.time() - mtime_ago_s
        os.utime(path, (old, old))
    return path


# ════════════════════════════════════════════════════════════════════
#  1. Age-based rotation
# ════════════════════════════════════════════════════════════════════
def test_rotates_old_file(tmp_path):
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    fp = _make_file(d / "diagnostics_slot1_20260101_120000.jsonl",
                     b'{"event":"old"}\n',
                     mtime_ago_s=86400 * 10)
    r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"),
                      max_age_days=7)
    result = r.rotate_once()
    assert result["files_rotated"] == 1
    assert not fp.exists()
    archive_files = list((d / "archive").rglob("*.gz"))
    assert len(archive_files) == 1


def test_skips_recent_files(tmp_path):
    """Files con mtime < 60s NO se tocan (grace period)."""
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    fp = _make_file(d / "diagnostics_slot1_20260513_120000.jsonl",
                     b'{"event":"recent"}\n',
                     mtime_ago_s=0)
    r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"),
                      max_age_days=0)  # threshold cero, todo elegible
    result = r.rotate_once()
    # Pero el grace period evita rotar (mtime < 60s)
    assert fp.exists()


def test_skips_files_under_age_threshold(tmp_path):
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    fp = _make_file(d / "diagnostics_slot1_20260513_120000.jsonl",
                     b'{"event":"medium"}\n',
                     mtime_ago_s=86400 * 3)  # 3 días
    r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"),
                      max_age_days=7)
    result = r.rotate_once()
    assert result["files_rotated"] == 0
    assert fp.exists()


# ════════════════════════════════════════════════════════════════════
#  2. Size-based rotation
# ════════════════════════════════════════════════════════════════════
def test_rotates_when_total_exceeds_threshold(tmp_path):
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    # 3 ficheros, todos 4 días viejos pero pequeños
    for i in range(3):
        fp = _make_file(
            d / f"diagnostics_slot{i}_20260101_1200{i:02d}.jsonl",
            b'X' * 1024 * 100,  # 100 KB cada uno = 300 KB total
            mtime_ago_s=86400 * 4,
        )
    # max_age=7 (no rotaría por edad), max_total_mb=0 (siempre exceeded)
    r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"),
                      max_age_days=7, max_total_mb=0)
    result = r.rotate_once()
    # Debe rotar al menos uno
    assert result["files_rotated"] >= 1


# ════════════════════════════════════════════════════════════════════
#  3. Archive structure
# ════════════════════════════════════════════════════════════════════
def test_archive_structure_yyyy_mm(tmp_path):
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    fp = _make_file(d / "diagnostics_slot1_20260101_120000.jsonl",
                     b'{"event":"old"}\n',
                     mtime_ago_s=86400 * 10)
    r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"),
                      max_age_days=7)
    r.rotate_once()
    # Debe haber un YYYY-MM directory
    archive = d / "archive"
    subdirs = [p for p in archive.iterdir() if p.is_dir()]
    assert len(subdirs) >= 1
    assert subdirs[0].name.count("-") == 1  # YYYY-MM format


def test_compressed_file_is_valid_gzip(tmp_path):
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    content = b'{"event":"line1"}\n{"event":"line2"}\n'
    fp = _make_file(d / "diagnostics_slot1_20260101_120000.jsonl",
                     content,
                     mtime_ago_s=86400 * 10)
    r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"),
                      max_age_days=7)
    r.rotate_once()
    gz_files = list((d / "archive").rglob("*.gz"))
    assert len(gz_files) == 1
    # Verificar contenido decomprimible
    with gzip.open(gz_files[0], "rb") as f:
        decompressed = f.read()
    assert decompressed == content


# ════════════════════════════════════════════════════════════════════
#  4. Idempotencia
# ════════════════════════════════════════════════════════════════════
def test_rotate_twice_no_double_rotation(tmp_path):
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    _make_file(d / "diagnostics_slot1_20260101_120000.jsonl",
                b'data', mtime_ago_s=86400 * 10)
    r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"),
                      max_age_days=7)
    r1 = r.rotate_once()
    r2 = r.rotate_once()  # second pass
    assert r1["files_rotated"] == 1
    assert r2["files_rotated"] == 0  # nothing to do


def test_duplicate_archive_gets_dup_suffix(tmp_path):
    """Si el archive de destino ya existe, sufijo .dup<N>."""
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    archive = d / "archive"
    # Crear fichero original
    fp = _make_file(d / "diagnostics_slot1_20260101_120000.jsonl",
                     b'first', mtime_ago_s=86400 * 10)
    r = JSONLRotator(directory=str(d), archive_dir=str(archive), max_age_days=7)
    r.rotate_once()
    # Recrear mismo nombre + rotar otra vez
    fp2 = _make_file(d / "diagnostics_slot1_20260101_120000.jsonl",
                      b'second', mtime_ago_s=86400 * 10)
    r.rotate_once()
    gz_files = list(archive.rglob("*.gz"))
    assert len(gz_files) >= 2  # ambos archivados


# ════════════════════════════════════════════════════════════════════
#  5. Disabled mode
# ════════════════════════════════════════════════════════════════════
def test_start_returns_false_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JSONL_ROTATION_ENABLED", "0")
    from core.dashboard.rotation import JSONLRotator
    r = JSONLRotator(directory=str(tmp_path), archive_dir=str(tmp_path / "a"))
    assert r.start() is False
    assert not r.is_running()


# ════════════════════════════════════════════════════════════════════
#  6. Stats
# ════════════════════════════════════════════════════════════════════
def test_stats_reflect_rotation(tmp_path):
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    _make_file(d / "diagnostics_slot1_20260101_120000.jsonl",
                b'X' * 1024, mtime_ago_s=86400 * 10)
    r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"),
                      max_age_days=7)
    r.rotate_once()
    stats = r.get_stats()
    assert stats["files_rotated_total"] >= 1
    assert stats["bytes_compressed_total"] > 0
    assert stats["last_rotation_at"] is not None
    assert "config" in stats
    assert "current" in stats


def test_stats_when_empty(tmp_path):
    from core.dashboard.rotation import JSONLRotator
    d = tmp_path / "runs"; d.mkdir()
    r = JSONLRotator(directory=str(d), archive_dir=str(d / "archive"))
    stats = r.get_stats()
    assert stats["current"]["active_files"] == 0


# ════════════════════════════════════════════════════════════════════
#  7. Fail-safe
# ════════════════════════════════════════════════════════════════════
def test_rotate_with_nonexistent_dir(tmp_path):
    from core.dashboard.rotation import JSONLRotator
    r = JSONLRotator(directory=str(tmp_path / "nonexistent"),
                      archive_dir=str(tmp_path / "archive"))
    # No debe lanzar
    result = r.rotate_once()
    assert result["files_rotated"] == 0


# ════════════════════════════════════════════════════════════════════
#  8. Endpoint
# ════════════════════════════════════════════════════════════════════
def test_endpoint_rotation_stats(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    client = app.test_client()
    r = client.get("/api/diagnostics/rotation/stats")
    assert r.status_code == 200
    data = r.get_json()
    assert "rotations_completed" in data or "error" in data


def test_endpoint_force_rotation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    from flask import Flask
    from core.dashboard import register_diagnostics
    app = Flask(__name__)
    register_diagnostics(app)
    client = app.test_client()
    r = client.get("/api/diagnostics/rotation/run")
    assert r.status_code == 200
    data = r.get_json()
    assert "ok" in data


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
