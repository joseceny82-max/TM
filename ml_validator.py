"""ml_validator.py — ML validator con dataset acumulable automáticamente.

Estrategia PRÁCTICA (no requiere 1000 capturas desde el día 1):

  1. Acumulador automático
     Hook a `akamai_bmjs.capture_sensor_data` → cada captura alimenta el dataset.
     El dataset crece solo con el uso normal del bot.

  2. Extractor de features (sin sklearn)
     Saca features numéricas de cada payload: total_events, ratios,
     entropy del b64, length per section, etc.

  3. Modelo estadístico simple (sin dependencias)
     Por cada feature calcula: mean, std, median, p05, p95 del dataset.
     Nuevos payloads se clasifican por Z-score contra estas distribuciones.

  4. Upgrade automático con sklearn (si está disponible)
     Si hay scikit-learn + dataset ≥ 100 samples, entrena IsolationForest
     que detecta outliers mejor que z-score.

  5. Feedback loop
     Cuando un payload lleva a compra EXITOSA (tm_purchase_flow.ok=True),
     se etiqueta como "PROVEN_GOOD" → se prioriza en el modelo.

API pública:
  add_to_dataset(payload, sensor_length, label="unknown")
  extract_features(payload, sensor_length)
  train_model()                   → stats del modelo actual
  classify(payload, sensor_length) → HUMAN | SUSPICIOUS | BOT + confidence
  get_dataset_stats()
  export_dataset() / import_dataset()
"""
import json
import math
import statistics
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any

import config
import json_atomic
import sqlite_store


_DATASET_KEY = "ml_validator_dataset.json"
_MODEL_KEY = "ml_validator_model.json"
_lock = threading.Lock()

MIN_SAMPLES_FOR_MODEL = 20       # umbral mínimo para entrenar modelo básico
MIN_SAMPLES_FOR_SKLEARN = 100    # umbral para upgrade a IsolationForest

# Features extraídas de cada payload
FEATURE_NAMES = [
    "total_events", "mouse_events", "key_events", "touch_events", "noise_events",
    "sensor_length", "sensor_entropy",
    "fp_hardware_concurrency", "fp_device_memory", "fp_plugins_count",
    "mouse_timing_cv", "key_timing_cv",
    "mouse_delta_avg", "mouse_dist_total",
    "touch_ratio", "noise_ratio",
]


# ═══════════════════════════════════════════════════════════
#  FEATURE EXTRACTION
# ═══════════════════════════════════════════════════════════
def extract_features(payload: dict, sensor_length: int = 0) -> Dict[str, float]:
    """Extrae las 16 features numéricas del payload."""
    features = {k: 0.0 for k in FEATURE_NAMES}

    mouse = payload.get("mouse_events", []) or []
    keys_e = payload.get("key_events", []) or []
    touch = payload.get("touch_events", []) or []
    noise = payload.get("noise_events", []) or []

    features["mouse_events"] = len(mouse)
    features["key_events"] = len(keys_e)
    features["touch_events"] = len(touch)
    features["noise_events"] = len(noise)
    features["total_events"] = len(mouse) + len(keys_e) + len(touch) + len(noise)
    features["sensor_length"] = sensor_length

    # Sensor entropy (si sensor_length dado, estimar)
    # En este punto no tenemos el sensor crudo, usamos estimación
    features["sensor_entropy"] = 0.0  # placeholder, se rellena en classify

    # Fingerprint features
    fp = payload.get("fingerprint", {}) or {}
    features["fp_hardware_concurrency"] = float(fp.get("hardwareConcurrency", 0))
    features["fp_device_memory"] = float(fp.get("deviceMemory", 0))
    features["fp_plugins_count"] = float(fp.get("plugins_count", 0))

    # Mouse timing CV (coeficiente de variación)
    if len(mouse) >= 3:
        dts = []
        for i in range(1, len(mouse)):
            if "t" in mouse[i] and "t" in mouse[i - 1]:
                dts.append(mouse[i]["t"] - mouse[i - 1]["t"])
        if len(dts) >= 2:
            try:
                std = statistics.stdev(dts)
                mean = statistics.mean(dts)
                features["mouse_timing_cv"] = round(std / max(1, mean), 4)
            except Exception:
                pass

    # Key timing CV
    if len(keys_e) >= 4:
        k_dts = []
        for i in range(1, len(keys_e)):
            if "t" in keys_e[i] and "t" in keys_e[i - 1]:
                k_dts.append(keys_e[i]["t"] - keys_e[i - 1]["t"])
        if len(k_dts) >= 2:
            try:
                k_std = statistics.stdev(k_dts)
                k_mean = statistics.mean(k_dts)
                features["key_timing_cv"] = round(k_std / max(1, k_mean), 4)
            except Exception:
                pass

    # Mouse delta/distance
    if mouse:
        deltas = []
        dist_total = 0.0
        for i, e in enumerate(mouse):
            if "dx" in e and "dy" in e:
                d = math.sqrt(e["dx"] ** 2 + e["dy"] ** 2)
                deltas.append(d)
                dist_total += d
        if deltas:
            features["mouse_delta_avg"] = round(statistics.mean(deltas), 2)
            features["mouse_dist_total"] = round(dist_total, 1)

    # Ratios
    total = features["total_events"]
    if total > 0:
        features["touch_ratio"] = round(features["touch_events"] / total, 4)
        features["noise_ratio"] = round(features["noise_events"] / total, 4)

    return features


# ═══════════════════════════════════════════════════════════
#  DATASET (acumulador)
# ═══════════════════════════════════════════════════════════
def _load_dataset() -> dict:
    return sqlite_store.load(config.ruta(_DATASET_KEY),
                              default={"samples": [], "labels_count": {}})


def _save_dataset(d: dict):
    sqlite_store.save(config.ruta(_DATASET_KEY), d)


def add_to_dataset(payload: dict,
                     sensor_length: int = 0,
                     label: str = "unknown",
                     source: str = "generated") -> dict:
    """Añade una muestra al dataset.

    label:  "human_confirmed" | "bot_confirmed" | "unknown" | "proven_good"
    source: "generated" | "captured" | "replay" | "proven_purchase"
    """
    features = extract_features(payload, sensor_length)
    sample = {
        "id": f"s{int(datetime.now().timestamp() * 1000)}",
        "ts": datetime.now().isoformat(),
        "label": label,
        "source": source,
        "features": features,
    }
    with _lock:
        d = _load_dataset()
        d["samples"].append(sample)
        # Cap a 10000 samples (suficiente para buen modelo)
        d["samples"] = d["samples"][-10000:]
        d["labels_count"][label] = d["labels_count"].get(label, 0) + 1
        _save_dataset(d)
    return {"ok": True, "sample_id": sample["id"],
             "dataset_size": len(d["samples"])}


def get_dataset_stats() -> dict:
    d = _load_dataset()
    samples = d.get("samples", [])
    by_label = d.get("labels_count", {})
    by_source = {}
    for s in samples:
        sr = s.get("source", "?")
        by_source[sr] = by_source.get(sr, 0) + 1
    return {
        "total_samples": len(samples),
        "by_label": by_label,
        "by_source": by_source,
        "min_for_model": MIN_SAMPLES_FOR_MODEL,
        "min_for_sklearn": MIN_SAMPLES_FOR_SKLEARN,
        "model_trainable": len(samples) >= MIN_SAMPLES_FOR_MODEL,
        "sklearn_trainable": len(samples) >= MIN_SAMPLES_FOR_SKLEARN,
        "last_sample_at": samples[-1].get("ts") if samples else None,
    }


def seed_from_captures() -> dict:
    """Importa capturas reales del pool akamai_bmjs como muestras 'human_confirmed'.
    Útil para bootstrap del dataset desde capturas ya existentes.
    """
    try:
        import akamai_bmjs
        captures = akamai_bmjs.list_captures(active_only=False)
    except Exception:
        return {"ok": False, "error": "akamai_bmjs no disponible"}

    added = 0
    for cap in captures:
        sensor = cap.get("sensor_data", "")
        if not sensor:
            continue
        # Decodificar el payload para obtener features reales
        parts = sensor.split(";")
        if len(parts) >= 4:
            try:
                from akamai_validator import _xor_decode
                key_hash = parts[2] if len(parts) == 5 else ""
                payload_b64 = parts[3] if len(parts) == 5 else parts[1]
                if key_hash:
                    decoded = _xor_decode(payload_b64, key_hash)
                    if decoded:
                        try:
                            payload = json.loads(decoded)
                            add_to_dataset(payload, sensor_length=len(sensor),
                                             label="human_confirmed",
                                             source="captured")
                            added += 1
                            continue
                        except Exception:
                            pass
                # Fallback: payload stub con features estimadas
                fake_payload = {
                    "mouse_events": [{}] * max(20, len(sensor) // 100),
                    "key_events": [{}] * max(5, len(sensor) // 300),
                }
                add_to_dataset(fake_payload, sensor_length=len(sensor),
                                 label="human_estimated", source="captured_stub")
                added += 1
            except Exception:
                continue

    return {"ok": True, "captures_found": len(captures), "added": added}


# ═══════════════════════════════════════════════════════════
#  MODEL TRAINING (statistics-based)
# ═══════════════════════════════════════════════════════════
def _compute_model_from_samples(samples: List[dict],
                                   filter_label: Optional[str] = None) -> dict:
    """Calcula distribuciones (mean, std, p05, p95) por feature."""
    if filter_label:
        samples = [s for s in samples if s.get("label") == filter_label]
    if not samples:
        return {"error": "no samples"}

    model = {
        "trained_at": datetime.now().isoformat(),
        "sample_count": len(samples),
        "filter_label": filter_label,
        "features": {},
    }

    for feat in FEATURE_NAMES:
        values = [s["features"].get(feat, 0) for s in samples]
        values = [v for v in values if v is not None]
        if not values:
            continue
        try:
            model["features"][feat] = {
                "mean": round(statistics.mean(values), 3),
                "std": round(statistics.stdev(values), 3) if len(values) > 1 else 0,
                "median": round(statistics.median(values), 3),
                "p05": round(sorted(values)[int(len(values) * 0.05)], 3) if values else 0,
                "p95": round(sorted(values)[int(len(values) * 0.95)], 3) if values else 0,
                "min": round(min(values), 3),
                "max": round(max(values), 3),
            }
        except Exception:
            pass

    return model


def train_model(prefer_label: str = "human_confirmed") -> dict:
    """Entrena el modelo estadístico. Prioriza samples 'human_confirmed',
    luego 'human_estimated', luego 'unknown'.
    """
    d = _load_dataset()
    samples = d.get("samples", [])
    if len(samples) < MIN_SAMPLES_FOR_MODEL:
        return {"ok": False,
                "error": f"dataset muy pequeño ({len(samples)} < {MIN_SAMPLES_FOR_MODEL})",
                "samples": len(samples)}

    # Preferencias de label
    for label in [prefer_label, "human_estimated", "proven_good", None]:
        m = _compute_model_from_samples(samples, filter_label=label)
        if "error" not in m and m.get("sample_count", 0) >= MIN_SAMPLES_FOR_MODEL:
            model_type = "statistical"

            # Upgrade a sklearn si está disponible + hay suficientes samples
            sklearn_available = False
            if m["sample_count"] >= MIN_SAMPLES_FOR_SKLEARN:
                try:
                    from sklearn.ensemble import IsolationForest
                    sklearn_available = True
                except ImportError:
                    pass

            if sklearn_available:
                try:
                    from sklearn.ensemble import IsolationForest
                    # Prepare X matrix
                    filtered = [s for s in samples if (not label or s.get("label") == label)]
                    X = [[s["features"].get(f, 0) for f in FEATURE_NAMES] for s in filtered]
                    iso = IsolationForest(contamination=0.1, random_state=42)
                    iso.fit(X)
                    # Guardar scores baseline
                    scores = iso.score_samples(X)
                    m["sklearn_iso_forest"] = {
                        "trained": True,
                        "score_mean": round(float(statistics.mean(scores)), 4),
                        "score_std": round(float(statistics.stdev(scores)) if len(scores) > 1 else 0, 4),
                    }
                    model_type = "isolation_forest+statistics"
                except Exception as e:
                    m["sklearn_error"] = str(e)[:60]

            m["model_type"] = model_type

            with _lock:
                json_atomic.save(config.ruta(_MODEL_KEY), m)
            return {"ok": True, "model": m}

    return {"ok": False, "error": "no hay suficientes samples con label reconocible"}


def get_model() -> dict:
    return json_atomic.load(config.ruta(_MODEL_KEY), default={})


# ═══════════════════════════════════════════════════════════
#  CLASSIFY (usa el modelo entrenado)
# ═══════════════════════════════════════════════════════════
def classify(payload: dict, sensor_length: int = 0,
              sensor_string: Optional[str] = None) -> dict:
    """Clasifica un payload con el modelo entrenado.

    Fallback si no hay modelo: usa EXPECTED_DISTRIBUTIONS básicas.
    """
    features = extract_features(payload, sensor_length)

    # Entropía del sensor crudo si se pasa
    if sensor_string:
        try:
            from akamai_validator import _shannon_entropy_simple
            features["sensor_entropy"] = round(_shannon_entropy_simple(sensor_string[:3000]), 3)
        except Exception:
            pass

    model = get_model()
    if not model or "features" not in model:
        # Fallback: usar ml_validate_payload clásico
        try:
            from akamai_validator import ml_validate_payload
            fallback = ml_validate_payload(payload, sensor_length)
            fallback["using_fallback"] = True
            fallback["reason"] = "no_model_trained_yet"
            return fallback
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Z-score por feature
    z_scores = {}
    anomaly_features = []
    for feat, stats in model["features"].items():
        val = features.get(feat, 0)
        mean = stats.get("mean", 0)
        std = stats.get("std", 1) or 0.1
        z = (val - mean) / max(0.1, std)
        z_scores[feat] = round(z, 2)
        # Flag si > 3σ del training set
        if abs(z) > 3:
            anomaly_features.append(feat)
        # Flag si fuera de rango p05-p95
        if val < stats.get("p05", 0) or val > stats.get("p95", float("inf")):
            if feat not in anomaly_features:
                anomaly_features.append(feat)

    # Human probability = inverse of avg abs z-score
    avg_abs_z = sum(abs(z) for z in z_scores.values()) / max(1, len(z_scores))
    human_prob = max(0, min(100, int(100 - avg_abs_z * 20)))

    # Verdict
    if human_prob >= 75:
        verdict = "HUMAN"
    elif human_prob >= 50:
        verdict = "LIKELY_HUMAN"
    elif human_prob >= 30:
        verdict = "SUSPICIOUS"
    else:
        verdict = "BOT"

    # Si sklearn está disponible y tenemos IsolationForest entrenado, añadir score
    iso_score = None
    if model.get("sklearn_iso_forest", {}).get("trained"):
        try:
            from sklearn.ensemble import IsolationForest
            # Re-entrenar con samples (caching mejoraría perf)
            d = _load_dataset()
            samples = [s for s in d.get("samples", [])
                        if s.get("label") == model.get("filter_label")]
            if samples:
                X = [[s["features"].get(f, 0) for f in FEATURE_NAMES] for s in samples]
                iso = IsolationForest(contamination=0.1, random_state=42)
                iso.fit(X)
                x_new = [[features.get(f, 0) for f in FEATURE_NAMES]]
                score = iso.score_samples(x_new)[0]
                predicted = iso.predict(x_new)[0]  # 1 = inlier, -1 = outlier
                iso_score = {
                    "score": round(float(score), 4),
                    "prediction": "inlier" if predicted == 1 else "outlier",
                    "baseline_mean": model["sklearn_iso_forest"]["score_mean"],
                }
                if predicted == -1:
                    # Outlier → ajustar verdict
                    if verdict in ("HUMAN", "LIKELY_HUMAN"):
                        verdict = "SUSPICIOUS"
                        human_prob = max(30, human_prob - 20)
        except Exception as e:
            iso_score = {"error": str(e)[:60]}

    return {
        "ok": True,
        "verdict": verdict,
        "human_probability_pct": human_prob,
        "avg_abs_z": round(avg_abs_z, 2),
        "z_scores": z_scores,
        "anomaly_features": anomaly_features,
        "features_computed": features,
        "isolation_forest": iso_score,
        "model_type": model.get("model_type", "statistical"),
        "model_samples": model.get("sample_count", 0),
    }


# ═══════════════════════════════════════════════════════════
#  FEEDBACK LOOP (tag proven purchases)
# ═══════════════════════════════════════════════════════════
def tag_successful_purchase(payload: dict, sensor_length: int = 0) -> dict:
    """Etiqueta un payload como 'proven_good' tras una compra exitosa.
    Se usa para re-entrenar el modelo con samples de alta calidad.
    """
    return add_to_dataset(payload, sensor_length,
                            label="proven_good", source="proven_purchase")


# ═══════════════════════════════════════════════════════════
#  EXPORT / IMPORT
# ═══════════════════════════════════════════════════════════
def export_dataset() -> dict:
    d = _load_dataset()
    return {
        "ok": True,
        "samples_count": len(d.get("samples", [])),
        "json_text": json.dumps(d, separators=(",", ":")),
        "export_ts": datetime.now().isoformat(),
    }


def import_dataset(json_text: str, merge: bool = True) -> dict:
    try:
        data = json.loads(json_text)
        incoming = data.get("samples", [])
        with _lock:
            if merge:
                existing = _load_dataset()
                existing_ids = {s.get("id") for s in existing.get("samples", [])}
                new_samples = [s for s in incoming if s.get("id") not in existing_ids]
                existing["samples"].extend(new_samples)
                existing["samples"] = existing["samples"][-10000:]
                # Actualizar labels_count
                for s in new_samples:
                    label = s.get("label", "unknown")
                    existing["labels_count"][label] = existing["labels_count"].get(label, 0) + 1
                _save_dataset(existing)
                return {"ok": True, "imported": len(new_samples),
                         "total": len(existing["samples"])}
            else:
                _save_dataset(data)
                return {"ok": True, "replaced": True,
                         "total": len(incoming)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════════════════════
#  SELF TEST
# ═══════════════════════════════════════════════════════════
def self_test() -> dict:
    """Test de extracción + training + clasificación."""
    results = {"ok": True, "checks": {}}
    import random

    # 1. Feature extraction
    fake_payload = {
        "mouse_events": [
            {"type": "mm", "x": random.randint(0, 1920), "y": random.randint(0, 1080),
             "t": i * random.randint(20, 80), "dx": random.randint(-20, 20),
             "dy": random.randint(-15, 15)}
            for i in range(40)
        ],
        "key_events": [
            {"type": "kd", "k": 65, "t": i * 150} for i in range(15)
        ],
        "touch_events": [],
        "fingerprint": {"hardwareConcurrency": 8, "deviceMemory": 16, "plugins_count": 5},
    }
    features = extract_features(fake_payload, sensor_length=5000)
    results["checks"]["features_extracted"] = len(features)
    results["checks"]["feature_mouse_timing_cv"] = features["mouse_timing_cv"]
    results["checks"]["feature_total_events"] = features["total_events"]

    # 2. Add samples al dataset
    initial_count = len(_load_dataset().get("samples", []))
    for i in range(25):
        # Variar payloads para simular dataset real
        p = dict(fake_payload)
        p["mouse_events"] = [{"t": j * random.randint(15, 120), "dx": random.randint(-30, 30),
                               "dy": random.randint(-20, 20)} for j in range(random.randint(30, 60))]
        add_to_dataset(p, sensor_length=random.randint(3000, 8000),
                        label="human_confirmed" if i < 20 else "unknown",
                        source="self_test")
    stats = get_dataset_stats()
    results["checks"]["dataset_grew"] = stats["total_samples"] > initial_count
    results["checks"]["model_trainable"] = stats["model_trainable"]

    # 3. Train model
    t = train_model()
    results["checks"]["model_trained"] = t.get("ok", False)
    if t.get("ok"):
        m = t["model"]
        results["checks"]["model_sample_count"] = m["sample_count"]
        results["checks"]["model_features_trained"] = len(m.get("features", {}))
        results["checks"]["model_type"] = m.get("model_type")

    # 4. Classify human-like payload
    c = classify(fake_payload, sensor_length=5000)
    results["checks"]["classify_verdict"] = c.get("verdict")
    results["checks"]["classify_human_prob"] = c.get("human_probability_pct")

    # 5. Classify bot-like payload (0 eventos)
    bot_payload = {"mouse_events": [], "key_events": [], "touch_events": [],
                    "fingerprint": {"hardwareConcurrency": 0}}
    cb = classify(bot_payload, sensor_length=200)
    results["checks"]["classify_bot_verdict"] = cb.get("verdict")

    # 6. Tag successful purchase
    tag = tag_successful_purchase(fake_payload, sensor_length=5000)
    results["checks"]["tag_purchase_ok"] = tag.get("ok", False)

    return results


def get_info() -> dict:
    model = get_model()
    ds_stats = get_dataset_stats()
    try:
        from sklearn.ensemble import IsolationForest  # noqa
        sklearn_available = True
    except ImportError:
        sklearn_available = False
    return {
        "module": "ml_validator",
        "dataset_stats": ds_stats,
        "model_trained": "features" in model,
        "model_type": model.get("model_type"),
        "model_sample_count": model.get("sample_count", 0),
        "model_features_count": len(model.get("features", {})),
        "sklearn_available": sklearn_available,
        "feature_names": FEATURE_NAMES,
    }
