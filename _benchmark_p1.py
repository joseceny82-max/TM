"""Benchmark P1 — JSONL handle cache antes/después.

Mide latencia p50/p95/p99 de RunSession.observe() y _compra_logger.log_event()
con cache ON vs OFF (kill switch).

Sin dependencias externas (solo stdlib). No es test, es script medible.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def percentiles(values: list[float]) -> dict:
    if not values:
        return {"p50": 0, "p95": 0, "p99": 0, "max": 0, "mean": 0}
    s = sorted(values)
    n = len(s)
    return {
        "n": n,
        "p50_ms": s[n // 2] * 1000,
        "p95_ms": s[int(n * 0.95)] * 1000,
        "p99_ms": s[int(n * 0.99)] * 1000,
        "max_ms": s[-1] * 1000,
        "mean_ms": statistics.mean(values) * 1000,
        "total_s": sum(values),
    }


def bench_run_session(n: int, cache_on: bool) -> dict:
    """Mide latencia observe() en RunSession."""
    os.environ["JSONL_HANDLE_CACHE_ENABLED"] = "1" if cache_on else "0"
    # Re-import to apply env
    for mod in list(sys.modules.keys()):
        if "run_session" in mod or "_compra_logger" in mod:
            del sys.modules[mod]
    from core.runtime.run_session import RunSession

    tmp = tempfile.mkdtemp()
    try:
        d = Path(tmp) / "runs"
        d.mkdir()
        s = RunSession(slot=99, run_id="bench",
                        jsonl_path=str(d / "diag.jsonl"))
        latencies: list[float] = []
        for i in range(n):
            t0 = time.perf_counter()
            s.observe(f"e{i}")
            latencies.append(time.perf_counter() - t0)
        s.finalize()  # cierra handle antes de rmtree
    finally:
        import shutil
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    return percentiles(latencies)


def bench_log_event(n: int, cache_on: bool) -> dict:
    """Mide latencia log_event() standalone."""
    os.environ["JSONL_HANDLE_CACHE_ENABLED"] = "1" if cache_on else "0"
    for mod in list(sys.modules.keys()):
        if "_compra_logger" in mod:
            del sys.modules[mod]
    import _compra_logger as cl
    cl._reset_handle_cache_for_test()

    tmp = tempfile.mkdtemp()
    try:
        d = Path(tmp) / "runs"
        d.mkdir()
        jpath = d / "log_event.jsonl"
        cl._LOG_DIR = d
        latencies: list[float] = []
        cl.log_event(slot=1, event="warm", jsonl_path=str(jpath))
        for i in range(n):
            t0 = time.perf_counter()
            cl.log_event(slot=1, event=f"e{i}", jsonl_path=str(jpath))
            latencies.append(time.perf_counter() - t0)
        # Importante: cerrar cache antes de rmtree (Windows file lock)
        cl._reset_handle_cache_for_test()
    finally:
        import shutil
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    return percentiles(latencies)


def main():
    N = 5000
    print(f"=== Benchmark P1 — N={N} writes ===\n")

    print("─── RunSession.observe() ───")
    off = bench_run_session(N, cache_on=False)
    on  = bench_run_session(N, cache_on=True)
    print(f"  CACHE OFF  p50={off['p50_ms']:.3f}ms  p95={off['p95_ms']:.3f}ms  "
          f"p99={off['p99_ms']:.3f}ms  total={off['total_s']:.2f}s")
    print(f"  CACHE ON   p50={on['p50_ms']:.3f}ms  p95={on['p95_ms']:.3f}ms  "
          f"p99={on['p99_ms']:.3f}ms  total={on['total_s']:.2f}s")
    if off['p95_ms'] > 0:
        speedup = off['p95_ms'] / max(on['p95_ms'], 0.0001)
        print(f"  → p95 speedup: {speedup:.2f}x")
    print()

    print("─── _compra_logger.log_event() ───")
    # stdout silenced para no inflar
    import io
    saved_stdout = sys.stdout
    try:
        sys.stdout = io.StringIO()
        off = bench_log_event(N, cache_on=False)
        on  = bench_log_event(N, cache_on=True)
    finally:
        sys.stdout = saved_stdout
    print(f"  CACHE OFF  p50={off['p50_ms']:.3f}ms  p95={off['p95_ms']:.3f}ms  "
          f"p99={off['p99_ms']:.3f}ms  total={off['total_s']:.2f}s")
    print(f"  CACHE ON   p50={on['p50_ms']:.3f}ms  p95={on['p95_ms']:.3f}ms  "
          f"p99={on['p99_ms']:.3f}ms  total={on['total_s']:.2f}s")
    if off['p95_ms'] > 0:
        speedup = off['p95_ms'] / max(on['p95_ms'], 0.0001)
        print(f"  → p95 speedup: {speedup:.2f}x")
    print()

    print("─── Criterio de aceptación P1 ───")
    print(f"  Target: p95 RunSession.observe() < 1.0ms con cache ON")
    print(f"  Real:   p95 = {on['p95_ms']:.3f}ms  {'✓ OK' if on['p95_ms'] < 1.0 else '✗ FAIL'}")


if __name__ == "__main__":
    main()
