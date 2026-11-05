"""auto_updater.py — Auto-actualización del sistema desde origin/main.

Proceso independiente del dashboard (sobrevive a sus reinicios). Cada ciclo:

  1. git fetch origin
  2. Si HEAD == origin/<branch>: ciclo termina (sin cambios).
  3. Si el working tree tiene modificaciones TRACKED: salta el ciclo
     (nunca pisamos cambios locales sin commitear).
  4. git pull --ff-only (si no es fast-forward, salta: requiere humano).
  5. Gate de calidad: pytest rápido (test_imports + test_smoke por defecto).
     Si falla -> git reset --hard <commit-previo> y NO se reinicia nada.
  6. Reinicia el dashboard (mata los python con 'dashboard.py' en su línea
     de comandos de ESTE proyecto y levanta uno nuevo) y espera a que
     /api/healthz responda ok.

Estado del último ciclo en logs/auto_updater_state.json; log en
logs/auto_updater.log.

Config (env): AUTO_UPDATE_INTERVAL_S (default 300), AUTO_UPDATE_BRANCH
(default main), AUTO_UPDATE_GATE (pytest args), AUTO_UPDATE_DRYRUN=1.

Uso:
    python auto_updater.py          # daemon en loop
    python auto_updater.py --once   # un solo ciclo (para pruebas/manual)
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict

PROYECTO = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PROYECTO, "logs")
LOG_PATH = os.path.join(LOG_DIR, "auto_updater.log")
STATE_PATH = os.path.join(LOG_DIR, "auto_updater_state.json")
PID_PATH = os.path.join(LOG_DIR, "auto_updater.pid")

INTERVAL_S = float(os.environ.get("AUTO_UPDATE_INTERVAL_S", "300"))
BRANCH = os.environ.get("AUTO_UPDATE_BRANCH", "main")
GATE = os.environ.get("AUTO_UPDATE_GATE",
                      "tests/test_imports.py tests/test_smoke.py")
DRYRUN = os.environ.get("AUTO_UPDATE_DRYRUN", "") in ("1", "true", "yes")
DASHBOARD_PORT = 5055


def _log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def _run(cmd: list, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=PROYECTO, capture_output=True,
                          text=True, timeout=timeout)


def _git(args: list, timeout: float = 120) -> subprocess.CompletedProcess:
    return _run(["git"] + args, timeout)


def _write_state(status: str, extra: Dict[str, Any] | None = None) -> None:
    st: Dict[str, Any] = {
        "checked_at": datetime.now().isoformat(),
        "status": status,
        "branch": BRANCH,
        "dryrun": DRYRUN,
    }
    if extra:
        st.update(extra)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
    except OSError as e:
        _log(f"WARN no pude escribir state: {e}")


def _tracked_dirty() -> bool:
    r = _git(["status", "--porcelain", "--untracked-files=no"])
    return bool(r.stdout.strip())


def _head() -> str:
    return _git(["rev-parse", "HEAD"]).stdout.strip()


def _version() -> str:
    try:
        with open(os.path.join(PROYECTO, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "?"


def _restart_dashboard() -> bool:
    """Mata los dashboard.py de este proyecto y levanta uno nuevo; espera healthz."""
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'dashboard\\.py' } | "
        "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; "
        "Write-Output (\"killed \" + $_.ProcessId) } catch {} }"
    )
    r = subprocess.run(["powershell", "-Command", ps_cmd],
                       capture_output=True, text=True, timeout=60)
    killed = [l for l in r.stdout.splitlines() if l.startswith("killed")]
    _log(f"dashboard reinicio: {len(killed)} proceso(s) detenido(s)")
    time.sleep(3)
    sp = subprocess.run([
        "powershell", "-Command",
        "Start-Process -FilePath 'python' -ArgumentList '-u','dashboard.py' "
        f"-WorkingDirectory '{PROYECTO}' -WindowStyle Hidden",
    ], capture_output=True, text=True, timeout=30)
    if sp.returncode != 0:
        _log(f"ERROR arrancando dashboard: {sp.stderr[:200]}")
        return False
    import urllib.request
    for i in range(12):
        time.sleep(5)
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{DASHBOARD_PORT}/api/healthz",
                    timeout=5) as resp:
                if resp.status == 200:
                    _log(f"dashboard respondio healthz tras {5*(i+1)}s")
                    return True
        except Exception:
            continue
    _log("ERROR dashboard no respondio healthz en 60s")
    return False


def cycle() -> Dict[str, Any]:
    """Un ciclo completo de auto-actualización. Devuelve resumen."""
    out: Dict[str, Any] = {}
    fr = _git(["fetch", "origin", BRANCH], timeout=180)
    if fr.returncode != 0:
        _log(f"ERROR fetch: {fr.stderr[:200]}")
        _write_state("fetch_error")
        return out
    local, remote = _head(), _git(["rev-parse", f"origin/{BRANCH}"]).stdout.strip()
    out.update(local=local[:8], remote=remote[:8], version=_version())
    if local == remote:
        _write_state("up_to_date", out)
        return out
    _log(f"hay cambios: local={local[:8]} -> origin={remote[:8]}")
    if _tracked_dirty():
        _log("SKIP: working tree con cambios tracked sin commitear")
        _write_state("skipped_dirty_tree", out)
        return out
    prev = local
    if DRYRUN:
        _log(f"DRYRUN: se habria actualizado a {remote[:8]}")
        _write_state("dryrun_would_update", out)
        return out
    pr = _git(["pull", "--ff-only", "origin", BRANCH], timeout=300)
    if pr.returncode != 0:
        _log(f"SKIP: pull no fast-forward: {pr.stderr[:200]}")
        _write_state("skipped_non_ff", out)
        return out
    gate = _run([sys.executable, "-m", "pytest"] + GATE.split() + ["-q"],
                timeout=600)
    if gate.returncode != 0:
        _log(f"GATE FALLÓ — rollback a {prev[:8]}. "
             f"stderr: {gate.stdout[-300:]}")
        _git(["reset", "--hard", prev])
        _write_state("rolled_back_gate_failed", out)
        return out
    _log(f"gate OK ({GATE}); nueva version {_version()}")
    if not _restart_dashboard():
        _write_state("updated_dashboard_restart_failed", out)
        return out
    _log(f"ACTUALIZADO {prev[:8]} -> {remote[:8]} v{_version()}")
    _write_state("updated", out)
    return out


def self_test() -> Dict[str, Any]:
    """Valida entorno del auto-updater (git, repo, paths escribibles)."""
    out: Dict[str, Any] = {"ok": True, "checks": {}}
    g = _git(["rev-parse", "--is-inside-work-tree"])
    out["checks"]["git_repo"] = g.returncode == 0 and g.stdout.strip() == "true"
    out["checks"]["remote_main"] = (
        _git(["rev-parse", "--verify", f"refs/remotes/origin/{BRANCH}"])
        .returncode == 0)
    out["checks"]["logs_dir"] = os.path.isdir(LOG_DIR)
    out["checks"]["gate_files_exist"] = all(
        os.path.exists(os.path.join(PROYECTO, p))
        for p in GATE.split() if p.endswith(".py"))
    out["checks"]["version_file"] = _version() not in ("", "?")
    out["ok"] = all(v is True for v in out["checks"].values())
    return out


def _pid_alive(pid: int) -> bool:
    r = subprocess.run(
        ["powershell", "-Command",
         f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue) -ne $null"],
        capture_output=True, text=True)
    return "True" in r.stdout


def _acquire_lock() -> bool:
    try:
        if os.path.exists(PID_PATH):
            with open(PID_PATH) as f:
                old = int(f.read().strip() or "0")
            if old and _pid_alive(old):
                return False
        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False


def main() -> int:
    if not _acquire_lock():
        _log("ya hay un auto_updater corriendo — exit")
        return 1
    once = "--once" in sys.argv
    try:
        st = self_test()
        _log(f"arranque v{_version()} once={once} intervalo={INTERVAL_S}s "
             f"self_test={st['ok']}")
        if not st["ok"]:
            _write_state("self_test_failed", st)
            return 1
        while True:
            try:
                cycle()
            except Exception as e:
                _log(f"ERROR ciclo: {type(e).__name__}: {e}")
                _write_state("cycle_error", {"error": str(e)[:200]})
            if once:
                break
            time.sleep(INTERVAL_S)
    finally:
        try:
            os.remove(PID_PATH)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
