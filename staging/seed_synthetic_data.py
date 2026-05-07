"""staging/seed_synthetic_data.py — Crea datos sintéticos para staging.

Genera:
  - 5 eventos sintéticos (Karol G demo, Taylor demo, etc.)
  - 3 cuentas TM sintéticas con tarjetas mock
  - 10 entries de audit_log
  - venue_cache con 2 venues
  - purchase_flows con 3 flows simulados

Output: directorio staging_data/ con todos los JSONs/SQLite necesarios.

Uso:
  python staging/seed_synthetic_data.py
  TM_PORT=5056 TM_DATA_DIR=staging_data python dashboard.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING_DIR = ROOT / "staging_data"
STAGING_DIR.mkdir(exist_ok=True)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def seed_events():
    events = [
        {
            "tm_id": f"staging_evt_{i:03d}",
            "name": name,
            "venue": venue,
            "city": city,
            "country": "ES",
            "sale_date": (datetime.now() + timedelta(days=i)).isoformat(),
            "tm_url": f"https://staging.example.com/event/{i}",
            "status": "scheduled",
            "enabled": True,
            "tags": ["staging", "synthetic"],
        }
        for i, (name, venue, city) in enumerate([
            ("Karol G — Demo", "Wanda Metropolitano", "Madrid"),
            ("Taylor Swift — Demo", "Spotify Camp Nou", "Barcelona"),
            ("Bad Bunny — Demo", "Estadio La Cartuja", "Sevilla"),
            ("Coldplay — Demo", "Estadio Olímpico", "Sevilla"),
            ("Rosalía — Demo", "Palau Sant Jordi", "Barcelona"),
        ], start=1)
    ]
    write_json(STAGING_DIR / "events.json", {"events": events})
    print(f"  [ok] events.json: {len(events)} eventos")


def seed_accounts():
    accounts = [
        {
            "id": f"staging_acc_{i}",
            "tm_email": f"staging{i}@example.invalid",
            "tm_password": "ENC1::staging_synthetic_pwd",
            "contact_email": f"staging_madre{i}@example.invalid",
            "alias": f"Staging User {i}",
            "country": "ES",
            "status": "active",
            "tags": ["staging"],
        }
        for i in range(1, 4)
    ]
    cards = [
        {
            "id": f"staging_card_{i}",
            "account_id": f"staging_acc_{i}",
            "label": f"Visa staging {i}",
            "card_number": "4111111111111111",
            "card_holder": f"STAGING USER {i}",
            "card_month": "12",
            "card_year": "2030",
            "card_cvv": "ENC1::123",
            "is_primary": True,
        }
        for i in range(1, 4)
    ]
    write_json(STAGING_DIR / "accounts.json", {"accounts": accounts, "cards": cards})
    print(f"  [ok] accounts.json: {len(accounts)} cuentas + {len(cards)} tarjetas")


def seed_purchases():
    flows = {}
    for i in range(1, 4):
        fid = f"staging_flow_{i:04d}"
        flows[fid] = {
            "started_at": (datetime.now() - timedelta(hours=i)).isoformat(),
            "steps": [
                {"step": "pick_proxy", "ok": True, "elapsed_ms": 120},
                {"step": "warm_session", "ok": True, "elapsed_ms": 850},
                {"step": "check_onsale_window", "ok": True, "elapsed_ms": 50},
                {"step": "simulate_cart", "ok": True, "elapsed_ms": 2400,
                 "data": {"cart_id": f"cart_{i:04d}"}},
            ],
        }
    write_json(STAGING_DIR / "purchase_flows.json", {"flows": flows})
    print(f"  [ok] purchase_flows.json: {len(flows)} flows")


def seed_audit_log():
    entries = []
    for i in range(10):
        entries.append({
            "ts": (datetime.now() - timedelta(minutes=i * 5)).isoformat(),
            "action": "auth:login_ok" if i % 2 else "purchase:flow_start",
            "actor": "staging_admin",
            "target": f"event_{i}",
            "ip": "127.0.0.1",
            "user_agent": "staging-seed",
            "meta": {"staging": True},
            "success": True,
        })
    write_json(STAGING_DIR / "audit_log.json", {"entries": entries})
    print(f"  [ok] audit_log.json: {len(entries)} entries")


def seed_venue_cache():
    venues = {
        "venue_madrid_metropolitano": {
            "name": "Wanda Metropolitano", "city": "Madrid", "country": "ES",
            "capacity": 68000,
            "sections": ["Pista", "Tribuna Norte", "Tribuna Sur", "Lateral"],
            "synthetic": True,
        },
        "venue_bcn_camp_nou": {
            "name": "Spotify Camp Nou", "city": "Barcelona", "country": "ES",
            "capacity": 99354,
            "sections": ["Pista", "Lateral 1", "Lateral 2", "Gol Norte"],
            "synthetic": True,
        },
    }
    write_json(STAGING_DIR / "venue_cache.json",
               {"venues": venues, "by_event": {}})
    print(f"  [ok] venue_cache.json: {len(venues)} venues")


def seed_sqlite():
    """Si el dashboard usa sqlite_store, también poblamos kv_store.db de staging."""
    db_path = STAGING_DIR / "kv_store.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL,
            version INTEGER DEFAULT 1
        )
    """)
    import time as _t
    now = _t.time()
    # Cargar los JSONs en kv_store
    for fname in ("purchase_flows", "venue_cache", "audit_log"):
        json_path = STAGING_DIR / f"{fname}.json"
        if json_path.exists():
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
            cur.execute(
                "INSERT OR REPLACE INTO kv_store (key, data, updated_at) VALUES (?, ?, ?)",
                (fname, json.dumps(data, ensure_ascii=False), now)
            )
    conn.commit()
    conn.close()
    print(f"  [ok] kv_store.db ({db_path.stat().st_size} bytes)")


def main():
    print(f"Seeding staging data en {STAGING_DIR}/")
    seed_events()
    seed_accounts()
    seed_purchases()
    seed_audit_log()
    seed_venue_cache()
    seed_sqlite()
    print("\nStaging environment listo.")
    print("Para arrancar: ")
    print("  TM_PORT=5056 TM_ENV=staging python dashboard.py")
    print("\nNota: Para que el dashboard lea de staging_data/ habría que")
    print("      modificar config.ruta() para honorar TM_DATA_DIR — esto")
    print("      es deliberadamente NO automático: staging y prod no deben")
    print("      compartir conf accidentalmente.")


if __name__ == "__main__":
    main()
