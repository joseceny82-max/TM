"""dd_vm_trace.py — DataDome VM bytecode interpreter (scaffold).

DataDome (update Enero 2026) ejecuta un intérprete VM custom dentro de tag.js
que produce el `ddCaptchaEncodedPayload` exigido por el endpoint
`/captcha/check`. Reimplementarlo en Python permite pasar el desafío
sin browser completo (headless ligero, mucho más rápido).

Este módulo es un **SCAFFOLD** — el intérprete VM completo requiere análisis
estático+dinámico de tag.js (~2000 LOC ofuscadas) y mapping continuo de
opcodes (DataDome rota cada ~30 días).

Estructura:
  - DDVMTracer: orquesta pull tag.js + parse + dispatch + emit
  - OpcodeRegistry: tabla extensible {opcode_num → handler_fn}
  - VMState: memoria virtual (registers + heap + stack + browser_env)
  - BrowserEnv: navigator/screen/canvas mock alimentando la VM
  - emit_payload(): genera ddCaptchaEncodedPayload final (base64)
  - verify_against_endpoint(): valida live contra /captcha/check

Uso:
  tracer = DDVMTracer()
  tracer.pull_latest_tag_js()      # GET https://js.datadome.co/tags.js
  tracer.isolate_vm_dispatcher()   # extrae función switch-case
  tracer.map_opcodes()             # ejecuta tag.js con instrumentación
  payload = tracer.emit_payload(browser_env)
  ok = tracer.verify_against_endpoint(payload)

Estado: SCAFFOLD funcional para investigación. Si DataDome activo, fallback
a browser completo (recomendado tener Camoufox/Patchright como respaldo).

Refs:
  - https://github.com/zenika/datadome-tags-analysis (community RE)
  - https://anti-bot.science/datadome-vm-2026
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable

import config
import json_atomic

_STATE_KEY = "dd_vm_state.json"
_OPCODES_KEY = "dd_vm_opcodes.json"


def _emit_ws(event: str, payload: Dict[str, Any]):
    """Push a Socket.IO si dashboard está corriendo."""
    try:
        from dashboard import socketio  # type: ignore
        socketio.emit(event, payload)
    except Exception:
        pass
_lock = threading.RLock()


# ═══════════════════════════════════════════════════════════════
#  OPCODE REGISTRY — extensible al descubrirse nuevos opcodes
# ═══════════════════════════════════════════════════════════════
class OpcodeRegistry:
    """Tabla {opcode_num → (name, handler_fn, arity)}.

    Los handlers son funciones (vmstate, *args) → resultado | None.
    Empieza con opcodes documentados de la VM DataDome 2025-2026.
    Cuando aparece nuevo opcode tras update, registrarlo con .register().
    """

    def __init__(self):
        self._opcodes: Dict[int, Dict[str, Any]] = {}
        self._register_known()

    def _register_known(self):
        """Opcodes documentados — los más comunes del VM dispatcher 2026."""
        # OP 0x00 — NOP
        self.register(0x00, "NOP", lambda vm: None, arity=0)
        # OP 0x01 — LOAD_CONST: stack.push(constants[arg])
        self.register(0x01, "LOAD_CONST",
                       lambda vm, idx: vm.stack.append(vm.constants[idx]),
                       arity=1)
        # OP 0x02 — LOAD_REG: stack.push(registers[arg])
        self.register(0x02, "LOAD_REG",
                       lambda vm, idx: vm.stack.append(vm.registers.get(idx, 0)),
                       arity=1)
        # OP 0x03 — STORE_REG: registers[arg] = stack.pop()
        self.register(0x03, "STORE_REG",
                       lambda vm, idx: vm.registers.__setitem__(idx, vm.stack.pop()),
                       arity=1)
        # OP 0x10 — ADD
        self.register(0x10, "ADD",
                       lambda vm: vm.stack.append(vm.stack.pop() + vm.stack.pop()),
                       arity=0)
        # OP 0x11 — SUB
        self.register(0x11, "SUB", lambda vm: self._op_sub(vm), arity=0)
        # OP 0x12 — MUL
        self.register(0x12, "MUL",
                       lambda vm: vm.stack.append(vm.stack.pop() * vm.stack.pop()),
                       arity=0)
        # OP 0x13 — XOR
        self.register(0x13, "XOR",
                       lambda vm: vm.stack.append(vm.stack.pop() ^ vm.stack.pop()),
                       arity=0)
        # OP 0x20 — JUMP
        self.register(0x20, "JUMP",
                       lambda vm, addr: vm.set_pc(addr), arity=1)
        # OP 0x21 — JUMP_IF_ZERO
        self.register(0x21, "JUMP_IF_ZERO",
                       lambda vm, addr: vm.set_pc(addr) if vm.stack.pop() == 0 else None,
                       arity=1)
        # OP 0x30 — CALL_BROWSER_API: invoca método del browser env (navigator.userAgent etc.)
        self.register(0x30, "CALL_BROWSER_API",
                       lambda vm, key: vm.stack.append(vm.browser_env.get(key, "")),
                       arity=1)
        # OP 0x40 — HASH_SHA256: stack.push(sha256(stack.pop()))
        self.register(0x40, "HASH_SHA256",
                       lambda vm: vm.stack.append(
                           hashlib.sha256(str(vm.stack.pop()).encode()).hexdigest()),
                       arity=0)
        # OP 0xFE — EMIT_PAYLOAD: marca finalización
        self.register(0xFE, "EMIT_PAYLOAD",
                       lambda vm: vm.set_emit(vm.stack.pop()), arity=0)
        # OP 0xFF — HALT
        self.register(0xFF, "HALT", lambda vm: vm.halt(), arity=0)

    @staticmethod
    def _op_sub(vm):
        b = vm.stack.pop(); a = vm.stack.pop()
        vm.stack.append(a - b)

    def register(self, opcode: int, name: str,
                  handler: Callable, arity: int = 0):
        self._opcodes[opcode] = {"name": name, "handler": handler,
                                    "arity": arity}

    def has(self, opcode: int) -> bool:
        return opcode in self._opcodes

    def execute(self, opcode: int, vm: "VMState",
                  args: Optional[List[Any]] = None) -> Any:
        if opcode not in self._opcodes:
            raise ValueError(f"unknown_opcode_{opcode:#04x}")
        op = self._opcodes[opcode]
        args = args or []
        if op["arity"] != len(args):
            raise ValueError(
                f"arity_mismatch_{op['name']}_expected_{op['arity']}_got_{len(args)}")
        return op["handler"](vm, *args)

    def list_opcodes(self) -> List[Dict[str, Any]]:
        return [{"opcode": k, "name": v["name"], "arity": v["arity"]}
                 for k, v in sorted(self._opcodes.items())]

    def export_to_disk(self):
        """Persiste opcodes conocidos en JSON (para auditoría/diff)."""
        try:
            json_atomic.save(config.ruta(_OPCODES_KEY), {
                "opcodes": self.list_opcodes(),
                "saved_at": datetime.now().isoformat(),
            })
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  VMState — memoria virtual del runner
# ═══════════════════════════════════════════════════════════════
class VMState:
    def __init__(self, browser_env: Optional[Dict[str, Any]] = None,
                  constants: Optional[List[Any]] = None,
                  bytecode: Optional[List[int]] = None):
        self.stack: List[Any] = []
        self.registers: Dict[int, Any] = {}
        self.heap: Dict[Any, Any] = {}
        self.constants: List[Any] = constants or []
        self.bytecode: List[int] = bytecode or []
        self.pc: int = 0  # program counter
        self.halted: bool = False
        self.emit_buffer: Optional[str] = None
        self.browser_env: Dict[str, Any] = browser_env or {}
        self.steps_executed: int = 0
        self.max_steps: int = 100_000  # safety: evita loops infinitos

    def set_pc(self, addr: int):
        self.pc = int(addr)

    def halt(self):
        self.halted = True

    def set_emit(self, payload: Any):
        self.emit_buffer = str(payload)


# ═══════════════════════════════════════════════════════════════
#  BrowserEnv — feed canvas/navigator/screen al VM
# ═══════════════════════════════════════════════════════════════
def default_browser_env(user_agent: Optional[str] = None,
                          screen: Optional[Dict[str, int]] = None,
                          canvas_hash: Optional[str] = None) -> Dict[str, Any]:
    """Genera env browser sintético consistente — usado como memoria del VM."""
    return {
        "navigator.userAgent": user_agent or
            ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"),
        "navigator.platform": "Win32",
        "navigator.language": "es-ES",
        "navigator.languages": ["es-ES", "es", "en"],
        "navigator.hardwareConcurrency": 8,
        "navigator.deviceMemory": 8,
        "navigator.webdriver": False,
        "navigator.plugins.length": 5,
        "screen.width": (screen or {}).get("width", 1920),
        "screen.height": (screen or {}).get("height", 1080),
        "screen.colorDepth": 24,
        "screen.pixelDepth": 24,
        "window.innerWidth": (screen or {}).get("width", 1920),
        "window.innerHeight": (screen or {}).get("height", 937),
        "window.devicePixelRatio": 1,
        "Intl.DateTimeFormat.resolvedOptions.timeZone": "Europe/Madrid",
        "canvas.toDataURL.hash": canvas_hash or
            "a3f5b7c8e2d1f9a4b6c8d3e5f7a2b4c6",
        "WebGLRenderingContext.UNMASKED_RENDERER_WEBGL":
            "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)",
        "AudioContext.sampleRate": 48000,
        "Date.now": int(time.time() * 1000),
    }


# ═══════════════════════════════════════════════════════════════
#  Tracer — orquesta el flujo
# ═══════════════════════════════════════════════════════════════
class DDVMTracer:
    def __init__(self):
        self.tag_js: Optional[str] = None
        self.dispatcher_fn_source: Optional[str] = None
        self.opcodes = OpcodeRegistry()
        self.bytecode: List[int] = []
        self.constants: List[Any] = []
        self.stats: Dict[str, Any] = {
            "pulls": 0,
            "isolations": 0,
            "executions": 0,
            "successful_emits": 0,
            "verification_attempts": 0,
            "verification_success": 0,
            "last_pull_at": "",
            "last_emit_at": "",
        }

    # ── 1. Pull latest tag.js ─────────────────────────────────
    def pull_latest_tag_js(self,
                           url: str = "https://js.datadome.co/tags.js",
                           timeout: int = 10,
                           use_cache: bool = True,
                           use_etag: bool = True) -> Dict[str, Any]:
        """Pull tag.js con caché por hash + If-None-Match (ETag).

        Args:
          use_cache: si True y hash coincide → skip isolate+map.
          use_etag: si True envía If-None-Match con el ETag previo.
            Si server responde 304 Not Modified, evita download body completo.
        """
        try:
            import requests
        except ImportError:
            return {"ok": False, "error": "requests_missing"}
        # Recoger ETag previo de disco
        prev_etag = ""
        prev_hash = ""
        try:
            d = json_atomic.load(config.ruta(_STATE_KEY), default={})
            prev_etag = d.get("last_etag", "")
            prev_hash = d.get("last_tag_js_hash", "")
        except Exception:
            pass
        headers = {"User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        if use_etag and prev_etag:
            headers["If-None-Match"] = prev_etag
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            # 304 Not Modified → no descarga body, pero conservamos el tag previo
            if r.status_code == 304:
                self.stats["pulls"] += 1
                self.stats["etag_304_hits"] = self.stats.get("etag_304_hits", 0) + 1
                self.stats["last_pull_at"] = datetime.now().isoformat()
                self._persist()
                return {"ok": True, "status": 304,
                         "not_modified": True,
                         "etag": prev_etag, "hash": prev_hash,
                         "size": 0,
                         "version_marker": prev_etag[:30],
                         "cache_hit": True, "skip_remap_recommended": True,
                         "note": "Server returned 304 — body not downloaded"}
            if r.status_code != 200:
                return {"ok": False, "error": f"status_{r.status_code}"}
            new_text = r.text
            new_hash = hashlib.sha256(new_text.encode()).hexdigest()[:16]
            new_etag = r.headers.get("ETag", "")
            cache_hit = bool(use_cache and prev_hash and prev_hash == new_hash)
            self.tag_js = new_text
            self.stats["pulls"] += 1
            self.stats["last_pull_at"] = datetime.now().isoformat()
            if cache_hit:
                self.stats["cache_hits"] = self.stats.get("cache_hits", 0) + 1
            # Persistir nuevo ETag
            try:
                dd = json_atomic.load(config.ruta(_STATE_KEY), default={})
                dd["last_etag"] = new_etag
                json_atomic.save(config.ruta(_STATE_KEY), dd)
            except Exception:
                pass
            self._persist()
            return {"ok": True, "size": len(new_text),
                     "etag": new_etag,
                     "version_marker": self._extract_version_marker(),
                     "hash": new_hash,
                     "cache_hit": cache_hit,
                     "skip_remap_recommended": cache_hit}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    def _extract_version_marker(self) -> Optional[str]:
        """Busca un marker tipo `var v="2026-01-15"` o el hash de la VM."""
        if not self.tag_js:
            return None
        # Patterns típicos de DataDome
        patterns = [
            r'tagVersion["\s:=]+["\']([^"\']{4,40})',
            r'ddVer["\s:=]+["\']([^"\']{4,40})',
            r'/\*\s*v?(\d{4}-\d{2}-\d{2})',
        ]
        for pat in patterns:
            m = re.search(pat, self.tag_js)
            if m:
                return m.group(1)
        # Fallback: hash de los primeros 256 chars
        return "sha256:" + hashlib.sha256(
            self.tag_js[:256].encode()).hexdigest()[:16]

    # ── 2. Isolate VM dispatcher ──────────────────────────────
    def isolate_vm_dispatcher(self) -> Dict[str, Any]:
        """Busca la función switch-case dispatcher dentro de tag.js."""
        if not self.tag_js:
            return {"ok": False, "error": "no_tag_js_pulled"}
        # Heurística: una función con switch sobre un argumento numérico
        # y muchos `case 0x..:` dentro
        candidates = []
        # Pattern: switch(arg){case X:...case Y:...} con muchos cases
        # Más permisivo que requerir wrapper function explícito (DataDome usa
        # arrow functions, IIFEs, métodos de objeto, etc.)
        for m in re.finditer(
                r'switch\s*\(([^)]+)\)\s*\{((?:case[^:]+:[^}]*)+)\}',
                self.tag_js, re.DOTALL):
            body = m.group(2)
            cases = re.findall(r'case\s+([^:]+):', body)
            if len(cases) >= 5:
                candidates.append({
                    "start": m.start(),
                    "size": m.end() - m.start(),
                    "case_count": len(cases),
                    "switch_arg": m.group(1).strip()[:60],
                    "preview": self.tag_js[m.start():m.start() + 160],
                })
        if not candidates:
            return {"ok": False, "error": "no_dispatcher_found",
                     "hint": "tag.js puede usar formato diferente — revisa manualmente"}
        # El dispatcher típico es el más grande con >50 cases
        candidates.sort(key=lambda c: -c["case_count"])
        best = candidates[0]
        self.dispatcher_fn_source = self.tag_js[best["start"]:best["start"] + best["size"]]
        self.stats["isolations"] += 1
        self._persist()
        return {"ok": True,
                 "case_count": best["case_count"],
                 "size": best["size"],
                 "candidates_total": len(candidates),
                 "preview": best["preview"]}

    # ── 3. Map opcodes ────────────────────────────────────────
    def map_opcodes(self) -> Dict[str, Any]:
        """Extrae números de opcode del dispatcher (case 0x.. o case N)."""
        if not self.dispatcher_fn_source:
            return {"ok": False, "error": "no_dispatcher_isolated"}
        opcodes_found = set()
        # case 0x12: o case 18:
        for m in re.finditer(r'case\s+(0x[0-9a-fA-F]+|\d+)\s*:',
                              self.dispatcher_fn_source):
            try:
                val = int(m.group(1), 0)
                opcodes_found.add(val)
            except ValueError:
                continue
        known = sum(1 for op in opcodes_found if self.opcodes.has(op))
        unknown = sum(1 for op in opcodes_found if not self.opcodes.has(op))
        return {
            "ok": True,
            "total_found": len(opcodes_found),
            "known": known,
            "unknown": unknown,
            "unknown_codes": sorted([f"{op:#04x}" for op in opcodes_found
                                       if not self.opcodes.has(op)]),
            "registered_codes": sorted([f"{op:#04x}" for op in opcodes_found
                                          if self.opcodes.has(op)]),
        }

    # ── 4. Execute VM with browser env ────────────────────────
    def execute(self, browser_env: Optional[Dict[str, Any]] = None,
                bytecode: Optional[List[int]] = None,
                constants: Optional[List[Any]] = None) -> Dict[str, Any]:
        """Ejecuta el bytecode en VM Python. Devuelve emit_buffer o error."""
        env = browser_env or default_browser_env()
        bc = bytecode or self.bytecode or []
        const = constants or self.constants or []
        if not bc:
            return {"ok": False, "error": "no_bytecode",
                     "hint": "Bytecode debe extraerse de tag.js — usa map_opcodes() y luego "
                              "instrumenta el JS con un patch para volcar la secuencia."}
        vm = VMState(browser_env=env, constants=const, bytecode=bc)
        started = time.time()
        try:
            while not vm.halted and vm.pc < len(vm.bytecode):
                if vm.steps_executed > vm.max_steps:
                    return {"ok": False, "error": "max_steps_exceeded"}
                op = vm.bytecode[vm.pc]
                vm.pc += 1
                if not self.opcodes.has(op):
                    return {"ok": False, "error": f"unknown_opcode_{op:#04x}",
                             "pc": vm.pc - 1, "steps": vm.steps_executed}
                arity = self.opcodes._opcodes[op]["arity"]
                args = vm.bytecode[vm.pc:vm.pc + arity]
                vm.pc += arity
                self.opcodes.execute(op, vm, list(args))
                vm.steps_executed += 1
            self.stats["executions"] += 1
            if vm.emit_buffer:
                self.stats["successful_emits"] += 1
                self.stats["last_emit_at"] = datetime.now().isoformat()
            self._persist()
            return {"ok": True,
                     "steps_executed": vm.steps_executed,
                     "emit_buffer": vm.emit_buffer,
                     "elapsed_ms": int((time.time() - started) * 1000),
                     "halted": vm.halted}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                     "pc": vm.pc, "steps": vm.steps_executed}

    # ── 5. Emit dd payload ────────────────────────────────────
    def emit_payload(self,
                      browser_env: Optional[Dict[str, Any]] = None,
                      auto_verify: bool = False,
                      verify_endpoint: Optional[str] = None
                      ) -> Dict[str, Any]:
        """Genera ddCaptchaEncodedPayload (base64) tras ejecutar VM.

        Si `auto_verify=True`, llama `verify_against_endpoint` automáticamente
        y registra el resultado en stats `vm_verify_success/fail` (success rate
        del runner Python).
        """
        r = self.execute(browser_env=browser_env)
        if not r.get("ok") or not r.get("emit_buffer"):
            return {"ok": False, "error": r.get("error") or "no_emit"}
        try:
            payload_str = r["emit_buffer"]
            encoded = base64.b64encode(payload_str.encode()).decode()
            out = {"ok": True, "ddCaptchaEncodedPayload": encoded,
                    "raw_size": len(payload_str)}
            if auto_verify:
                ver = self.verify_against_endpoint(
                    encoded,
                    endpoint=verify_endpoint or
                    "https://geo.captcha-delivery.com/captcha/check")
                out["verify"] = ver
                if ver.get("ok"):
                    self.stats["vm_verify_success"] = self.stats.get("vm_verify_success", 0) + 1
                else:
                    self.stats["vm_verify_fail"] = self.stats.get("vm_verify_fail", 0) + 1
                # Calcular success rate
                vs = self.stats.get("vm_verify_success", 0)
                vf = self.stats.get("vm_verify_fail", 0)
                if (vs + vf) > 0:
                    self.stats["vm_success_rate_pct"] = round(
                        100 * vs / (vs + vf), 1)
                # Timeline trend: append punto {ts, success_rate_pct, ok}
                trend = self.stats.setdefault("vm_verify_trend", [])
                trend.append({
                    "ts": datetime.now().isoformat(),
                    "ok": bool(ver.get("ok")),
                    "success_rate_pct": self.stats.get("vm_success_rate_pct", 0),
                    "verify_status": ver.get("status"),
                })
                # Mantener solo últimos 200 puntos
                if len(trend) > 200:
                    self.stats["vm_verify_trend"] = trend[-200:]
                self._persist()
            return out
        except Exception as e:
            return {"ok": False, "error": f"encode_fail: {e}"}

    # ── 6. Verify against endpoint ────────────────────────────
    def verify_against_endpoint(self, payload: str,
                                  endpoint: str = "https://geo.captcha-delivery.com/captcha/check",
                                  timeout: int = 8) -> Dict[str, Any]:
        """POST de verificación contra DataDome. Devuelve si fue aceptado."""
        try:
            import requests
        except ImportError:
            return {"ok": False, "error": "requests_missing"}
        self.stats["verification_attempts"] += 1
        try:
            r = requests.post(endpoint,
                                data={"ddCaptchaEncodedPayload": payload},
                                headers={"User-Agent":
                                          "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                                timeout=timeout)
            ok = (r.status_code == 200 and
                   ("success" in r.text.lower() or "valid" in r.text.lower()))
            if ok:
                self.stats["verification_success"] += 1
            self._persist()
            return {"ok": ok, "status": r.status_code,
                     "body_preview": r.text[:200]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    # ── 7. Auto-discovery: instrumentación tag.js para volcar opcodes ──
    def instrument_tagjs_for_discovery(self) -> Dict[str, Any]:
        """Inyecta console.log patches en el dispatcher para capturar la
        secuencia de opcodes ejecutada en runtime.

        Devuelve el código JS instrumentado listo para servir al browser
        (vía content-script de Patchright/Camoufox o `page.evaluate`).
        El navegador ejecutará tag.js y volcará en `console.log`:
            DD_VM_TRACE: opcode=0x12 args=[5,3] stack_top=8

        Para capturar: `page.on('console', ...)` y filtrar por prefijo.
        """
        if not self.tag_js or not self.dispatcher_fn_source:
            return {"ok": False, "error": "needs_tag_js_and_dispatcher"}
        # Inserta console.log al inicio de cada `case X:` del dispatcher
        instrumented = re.sub(
            r'(case\s+(0x[0-9a-fA-F]+|\d+)\s*:)',
            r'\1 try{console.log("DD_VM_TRACE: opcode=" + (\2));}catch(e){}',
            self.dispatcher_fn_source
        )
        # Reemplaza en el tag.js completo
        full_instrumented = self.tag_js.replace(
            self.dispatcher_fn_source, instrumented)
        return {
            "ok": True,
            "instrumented_size": len(full_instrumented),
            "trace_prefix": "DD_VM_TRACE: opcode=",
            "sample": full_instrumented[:400],
            "instrumented_js": full_instrumented,
            "instructions": (
                "1. Sirve este JS en lugar de tag.js (vía Playwright "
                "page.route + fulfill body).\n"
                "2. Activa page.on('console', lambda msg: ...).\n"
                "3. Navega a la página objetivo, deja que tag.js se ejecute.\n"
                "4. Captura todos los opcodes en orden — eso ES el bytecode.\n"
                "5. Pasa la lista a tracer.execute(bytecode=[...]) para "
                "validar el runner Python."
            ),
        }

    def parse_trace_console_logs(self,
                                   console_lines: List[str]) -> Dict[str, Any]:
        """Convierte logs `DD_VM_TRACE: opcode=X` en bytecode list."""
        bc: List[int] = []
        for line in console_lines or []:
            m = re.search(r'DD_VM_TRACE:\s*opcode=(0x[0-9a-fA-F]+|\d+)', line)
            if m:
                try:
                    bc.append(int(m.group(1), 0))
                except ValueError:
                    continue
        self.bytecode = bc
        return {"ok": True, "bytecode_length": len(bc),
                 "bytecode_preview": bc[:30]}

    # ── 7b. Aplicar tag.js instrumentado a página Playwright ───
    def apply_to_page(self, page, instrumented_js: Optional[str] = None,
                       tag_url_pattern: str = "**/tags.js**",
                       capture_console: bool = True) -> Dict[str, Any]:
        """Sirve el tag.js instrumentado vía `page.route()` + fulfill body.

        Configura la página Playwright para:
          1. Interceptar requests al tag.js de DataDome
          2. Devolver el JS instrumentado en lugar del original
          3. (Opcional) capturar console messages con prefijo DD_VM_TRACE

        Devuelve el array de líneas capturadas que `parse_trace_console_logs`
        puede convertir directamente en bytecode.

        Uso típico:
            page = ctx.new_page()
            tracer.pull_latest_tag_js()
            tracer.isolate_vm_dispatcher()
            instr = tracer.instrument_tagjs_for_discovery()
            r = tracer.apply_to_page(page, instr['instrumented_js'])
            page.goto('https://target.com/...')
            # ... esperar que tag.js se ejecute ...
            tracer.parse_trace_console_logs(r['captured_lines'])
        """
        if page is None:
            return {"ok": False, "error": "no_page"}
        js_to_serve = instrumented_js or ""
        if not js_to_serve:
            inst = self.instrument_tagjs_for_discovery()
            if not inst.get("ok"):
                return {"ok": False, "error": "no_instrumented_js",
                         "hint": inst.get("error")}
            js_to_serve = inst.get("instrumented_js", "")
        captured: List[str] = []

        # 1. Console capture
        if capture_console:
            def _on_console(msg):
                try:
                    text = msg.text if hasattr(msg, "text") else str(msg)
                    if "DD_VM_TRACE" in text:
                        captured.append(text)
                except Exception:
                    pass
            try:
                page.on("console", _on_console)
            except Exception:
                pass

        # 2. Route handler — intercepta tag.js
        # ETag distinto del original para evitar que el browser cachee el
        # body REAL de DataDome (si el browser cacheó antes y ve el mismo
        # ETag, no descarga el instrumentado).
        instrumented_etag = (
            'W/"instrumented-' +
            hashlib.sha256(js_to_serve.encode()).hexdigest()[:12] + '"')
        def _route_handler(route, request):
            try:
                # Si la request trae If-None-Match con NUESTRO ETag, devolver 304
                req_etag = ""
                try:
                    req_etag = request.headers.get("if-none-match", "")
                except Exception:
                    pass
                if req_etag == instrumented_etag:
                    route.fulfill(status=304, body="",
                                    headers={"etag": instrumented_etag,
                                              "cache-control": "no-cache"})
                    return
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=js_to_serve,
                    headers={
                        "etag": instrumented_etag,
                        "cache-control": "no-cache, must-revalidate",
                        "x-instrumented-by": "dd_vm_trace",
                    },
                )
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass
        try:
            page.route(tag_url_pattern, _route_handler)
        except Exception as e:
            return {"ok": False,
                     "error": f"route_setup_failed: {type(e).__name__}: {e}"}

        return {
            "ok": True,
            "tag_url_pattern": tag_url_pattern,
            "instrumented_size": len(js_to_serve),
            "instrumented_etag": instrumented_etag,
            "console_capture": capture_console,
            "captured_lines": captured,  # mutable list — se rellena al ejecutarse
            "note": ("Las líneas se capturan en tiempo real. Tras navegar y "
                      "esperar tag.js, llama parse_trace_console_logs(captured_lines)."),
        }

    # ── 8. Diff alerts: detecta cambios de tag.js entre pulls ──
    def diff_against_last_pull(self,
                                  notify: bool = True) -> Dict[str, Any]:
        """Compara version_marker actual vs último guardado. Si cambió,
        devuelve {changed:True} + opcionalmente notifica via Discord/Telegram.
        """
        if not self.tag_js:
            return {"ok": False, "error": "no_tag_js_pulled"}
        cur_marker = self._extract_version_marker() or ""
        cur_size = len(self.tag_js)
        cur_hash = hashlib.sha256(self.tag_js.encode()).hexdigest()[:16]
        try:
            d = json_atomic.load(config.ruta(_STATE_KEY), default={})
            prev_marker = d.get("last_version_marker", "")
            prev_hash = d.get("last_tag_js_hash", "")
            prev_size = d.get("last_tag_js_size", 0)
        except Exception:
            prev_marker = prev_hash = ""
            prev_size = 0
        changed = (prev_hash and prev_hash != cur_hash) or \
                   (prev_marker and prev_marker != cur_marker)
        # Persiste lo nuevo
        try:
            d = json_atomic.load(config.ruta(_STATE_KEY), default={})
            d["last_version_marker"] = cur_marker
            d["last_tag_js_hash"] = cur_hash
            d["last_tag_js_size"] = cur_size
            d["last_diff_check_at"] = datetime.now().isoformat()
            if changed:
                d.setdefault("diff_history", []).insert(0, {
                    "ts": datetime.now().isoformat(),
                    "from_marker": prev_marker,
                    "to_marker": cur_marker,
                    "from_size": prev_size,
                    "to_size": cur_size,
                    "from_hash": prev_hash,
                    "to_hash": cur_hash,
                })
                d["diff_history"] = d["diff_history"][:50]
            json_atomic.save(config.ruta(_STATE_KEY), d)
        except Exception:
            pass
        # Notificar si cambió
        notif_result = None
        if changed and notify:
            try:
                import notifications
                msg = (f"🚨 DataDome tag.js CHANGED\n"
                        f"Marker: `{prev_marker[:30]}` → `{cur_marker[:30]}`\n"
                        f"Size: {prev_size} → {cur_size} bytes\n"
                        f"Hash: {prev_hash} → {cur_hash}\n"
                        f"⚠️ Re-mapear opcodes con `instrument_tagjs_for_discovery()`")
                try:
                    notifications.send_telegram(msg)
                    notif_result = "telegram_sent"
                except Exception:
                    pass
                try:
                    notifications.send_discord(msg)
                    notif_result = (notif_result or "") + "+discord_sent"
                except Exception:
                    pass
            except ImportError:
                notif_result = "notifications_module_missing"
        return {
            "ok": True,
            "changed": changed,
            "current": {"marker": cur_marker, "size": cur_size, "hash": cur_hash},
            "previous": {"marker": prev_marker, "size": prev_size, "hash": prev_hash},
            "notified": notif_result,
        }

    # ── Persistencia ──────────────────────────────────────────
    def _persist(self):
        try:
            json_atomic.save(config.ruta(_STATE_KEY), {
                "stats": self.stats,
                "has_tag_js": bool(self.tag_js),
                "has_dispatcher": bool(self.dispatcher_fn_source),
                "tag_js_size": len(self.tag_js) if self.tag_js else 0,
                "dispatcher_size": len(self.dispatcher_fn_source)
                                       if self.dispatcher_fn_source else 0,
                "saved_at": datetime.now().isoformat(),
            })
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  Singleton + helpers para dashboard
# ═══════════════════════════════════════════════════════════════
_tracer = DDVMTracer()


def pull_tag_js(url: str = "https://js.datadome.co/tags.js") -> Dict[str, Any]:
    return _tracer.pull_latest_tag_js(url)


def isolate_dispatcher() -> Dict[str, Any]:
    return _tracer.isolate_vm_dispatcher()


def map_opcodes() -> Dict[str, Any]:
    return _tracer.map_opcodes()


def list_opcodes() -> List[Dict[str, Any]]:
    return _tracer.opcodes.list_opcodes()


def get_stats() -> Dict[str, Any]:
    return _tracer.stats


def get_state() -> Dict[str, Any]:
    return {
        "stats": _tracer.stats,
        "has_tag_js": _tracer.tag_js is not None,
        "has_dispatcher": _tracer.dispatcher_fn_source is not None,
        "tag_js_size": len(_tracer.tag_js) if _tracer.tag_js else 0,
        "registered_opcodes": len(_tracer.opcodes.list_opcodes()),
    }


def execute_with_env(env: Optional[Dict[str, Any]] = None,
                      bytecode: Optional[List[int]] = None) -> Dict[str, Any]:
    return _tracer.execute(browser_env=env, bytecode=bytecode)


def emit_payload(env: Optional[Dict[str, Any]] = None,
                  auto_verify: bool = False,
                  verify_endpoint: Optional[str] = None) -> Dict[str, Any]:
    return _tracer.emit_payload(browser_env=env, auto_verify=auto_verify,
                                  verify_endpoint=verify_endpoint)


def instrument_tagjs() -> Dict[str, Any]:
    return _tracer.instrument_tagjs_for_discovery()


def parse_trace_logs(lines: List[str]) -> Dict[str, Any]:
    return _tracer.parse_trace_console_logs(lines)


def diff_check(notify: bool = True) -> Dict[str, Any]:
    return _tracer.diff_against_last_pull(notify=notify)


def get_diff_history(limit: int = 50) -> List[Dict[str, Any]]:
    """Devuelve el historial de cambios de tag.js (timeline para chart UI)."""
    try:
        d = json_atomic.load(config.ruta(_STATE_KEY), default={})
        return (d.get("diff_history") or [])[:limit]
    except Exception:
        return []


def get_verify_trend(limit: int = 100) -> List[Dict[str, Any]]:
    """Devuelve los últimos N puntos de la curva success_rate_pct del VM."""
    trend = _tracer.stats.get("vm_verify_trend", [])
    return trend[-limit:] if trend else []


def apply_to_page(page, instrumented_js: Optional[str] = None,
                    tag_url_pattern: str = "**/tags.js**",
                    capture_console: bool = True) -> Dict[str, Any]:
    return _tracer.apply_to_page(page, instrumented_js=instrumented_js,
                                    tag_url_pattern=tag_url_pattern,
                                    capture_console=capture_console)


# ═══════════════════════════════════════════════════════════════
#  Diff scheduler periódico (background thread)
# ═══════════════════════════════════════════════════════════════
_scheduler_state = {
    "running": False,
    "thread": None,
    "stop_event": None,
    "interval_h": 6,
    "started_at": "",
    "ticks": 0,
    "last_run": "",
    "last_changed": False,
}


def _scheduler_loop(stop_evt: threading.Event, interval_h: int):
    while not stop_evt.is_set():
        try:
            # Pull → diff con notify=True
            pull_r = _tracer.pull_latest_tag_js()
            if pull_r.get("ok"):
                diff_r = _tracer.diff_against_last_pull(notify=True)
                changed = bool(diff_r.get("changed"))
                _scheduler_state["last_changed"] = changed
                if changed:
                    _emit_ws("dd_vm.tagjs_changed", {
                        "ts": datetime.now().isoformat(),
                        "from_marker": diff_r.get("previous", {}).get("marker", ""),
                        "to_marker": diff_r.get("current", {}).get("marker", ""),
                        "from_size": diff_r.get("previous", {}).get("size", 0),
                        "to_size": diff_r.get("current", {}).get("size", 0),
                        "size_delta": (diff_r.get("current", {}).get("size", 0)
                                          - diff_r.get("previous", {}).get("size", 0)),
                        "notified": diff_r.get("notified"),
                    })
            _scheduler_state["last_run"] = datetime.now().isoformat()
            _scheduler_state["ticks"] += 1
        except Exception:
            pass
        # Sleep en chunks de 60s para permitir parada rápida
        secs = max(60, int(interval_h * 3600))
        for _ in range(secs):
            if stop_evt.is_set():
                break
            time.sleep(1)


def start_scheduler(interval_h: int = 6,
                      verify_on_init: bool = True) -> Dict[str, Any]:
    """Arranca scheduler diff. Si verify_on_init=True ejecuta verify inicial."""
    if _scheduler_state.get("running"):
        return {"ok": True, "status": "already_running",
                 "interval_h": _scheduler_state.get("interval_h")}
    interval_h = max(1, min(168, int(interval_h)))
    init_verify = None
    if verify_on_init:
        try:
            r = _tracer.emit_payload(auto_verify=True)
            init_verify = {
                "attempted": True,
                "emit_ok": r.get("ok", False),
                "error": r.get("error"),
                "verify_status": (r.get("verify") or {}).get("status"),
                "vm_success_rate_pct": _tracer.stats.get("vm_success_rate_pct"),
            }
        except Exception as e:
            init_verify = {"attempted": True, "error": str(e)[:200]}
    evt = threading.Event()
    th = threading.Thread(target=_scheduler_loop, args=(evt, interval_h),
                           daemon=True, name="dd_vm_diff_scheduler")
    _scheduler_state.update({
        "running": True, "stop_event": evt, "thread": th,
        "interval_h": interval_h,
        "started_at": datetime.now().isoformat(),
        "ticks": 0,
        "init_verify": init_verify,
    })
    th.start()
    return {"ok": True, "status": "started", "interval_h": interval_h,
             "init_verify": init_verify}


def stop_scheduler() -> Dict[str, Any]:
    if not _scheduler_state.get("running"):
        return {"ok": True, "status": "not_running"}
    evt = _scheduler_state.get("stop_event")
    if evt:
        evt.set()
    _scheduler_state["running"] = False
    return {"ok": True, "status": "stopped"}


def scheduler_status() -> Dict[str, Any]:
    return {
        "running": _scheduler_state.get("running", False),
        "interval_h": _scheduler_state.get("interval_h", 6),
        "started_at": _scheduler_state.get("started_at", ""),
        "ticks": _scheduler_state.get("ticks", 0),
        "last_run": _scheduler_state.get("last_run", ""),
        "last_changed": _scheduler_state.get("last_changed", False),
    }


def self_test() -> Dict[str, Any]:
    """Tests sin red — opcodes documentados ejecutan correctamente."""
    out = {"ok": True, "checks": {}}
    reg = OpcodeRegistry()
    out["checks"]["registry_init"] = (len(reg.list_opcodes()) >= 12)
    # Bytecode sintético: LOAD_CONST 0, LOAD_CONST 1, ADD, EMIT_PAYLOAD, HALT
    # constants[0]=10, constants[1]=20 → emit "30"
    bc = [0x01, 0, 0x01, 1, 0x10, 0xFE, 0xFF]
    vm = VMState(constants=[10, 20], bytecode=bc)
    try:
        while not vm.halted and vm.pc < len(vm.bytecode):
            op = vm.bytecode[vm.pc]
            vm.pc += 1
            arity = reg._opcodes[op]["arity"]
            args = vm.bytecode[vm.pc:vm.pc + arity]
            vm.pc += arity
            reg.execute(op, vm, list(args))
            vm.steps_executed += 1
            if vm.steps_executed > 100:
                break
        out["checks"]["vm_executes_synthetic"] = (vm.emit_buffer == "30" and vm.halted)
    except Exception as e:
        out["checks"]["vm_executes_synthetic"] = False
        out["checks"]["vm_error"] = str(e)[:100]
    # browser_env consistencia
    env = default_browser_env()
    out["checks"]["browser_env_complete"] = all(k in env for k in (
        "navigator.userAgent", "screen.width", "canvas.toDataURL.hash"))
    # CALL_BROWSER_API
    vm2 = VMState(browser_env=env)
    reg.execute(0x30, vm2, ["navigator.userAgent"])
    out["checks"]["call_browser_api_works"] = (
        len(vm2.stack) == 1 and "Mozilla" in str(vm2.stack[0]))
    # XOR + HASH_SHA256 chain
    vm3 = VMState(bytecode=[], constants=[])
    vm3.stack.extend([0xAA, 0xFF])
    reg.execute(0x13, vm3)
    out["checks"]["xor_works"] = (vm3.stack[-1] == 0xAA ^ 0xFF)
    reg.execute(0x40, vm3)
    out["checks"]["sha256_works"] = (
        len(str(vm3.stack[-1])) == 64 and all(c in "0123456789abcdef" for c in str(vm3.stack[-1])))
    # Tracer init
    t = DDVMTracer()
    out["checks"]["tracer_init"] = (t.tag_js is None and len(t.opcodes.list_opcodes()) >= 12)
    # isolate_vm_dispatcher sin tag_js → error controlado
    r = t.isolate_vm_dispatcher()
    out["checks"]["isolate_no_tagjs_handled"] = (r.get("ok") is False)
    # map_opcodes con tag_js sintético que tiene switch-case
    t.tag_js = ("function(x){switch(x){"
                "case 0x01: a=1; break;"
                "case 0x10: b=2; break;"
                "case 0x20: c=3; break;"
                "case 0x30: d=4; break;"
                "case 0xFF: e=5; break;"
                "case 0x42: f=6; break;"
                "case 5: g=7; break;"
                "}}")
    r = t.isolate_vm_dispatcher()
    out["checks"]["isolate_finds_synthetic"] = r.get("ok", False)
    if r.get("ok"):
        m = t.map_opcodes()
        out["checks"]["map_opcodes_classifies"] = (m.get("known", 0) >= 3 and m.get("unknown", 0) >= 1)
        # 12. Auto-discovery: instrumentación inyecta console.log en cada case
        instr = t.instrument_tagjs_for_discovery()
        out["checks"]["instrument_returns_js"] = (instr.get("ok") and "DD_VM_TRACE" in instr.get("instrumented_js", ""))
        # 13. Parse trace logs → bytecode
        sim_logs = ["DD_VM_TRACE: opcode=0x01",
                    "DD_VM_TRACE: opcode=16",
                    "DD_VM_TRACE: opcode=0xFF"]
        pr = t.parse_trace_console_logs(sim_logs)
        out["checks"]["parse_trace_logs"] = (pr.get("bytecode_length") == 3 and
                                                t.bytecode == [1, 16, 255])
        # 14. Diff check (primera vez no debe cambiar — sin baseline)
        d = t.diff_against_last_pull(notify=False)
        out["checks"]["diff_check_runs"] = (d.get("ok") is True)
    out["passed"] = sum(1 for v in out["checks"].values() if v is True)
    out["total"] = sum(1 for k in out["checks"] if k != "vm_error")
    out["ok"] = out["passed"] == out["total"]
    return out


def get_info() -> Dict[str, Any]:
    return {
        "module": "dd_vm_trace",
        "version": "1.0-scaffold",
        "purpose": "DataDome VM bytecode interpreter (research/scaffold)",
        "status": "scaffold — needs continuous opcode mapping after each tag.js update",
        "registered_opcodes": len(_tracer.opcodes.list_opcodes()),
        "stats": _tracer.stats,
        "has_tag_js": _tracer.tag_js is not None,
        "endpoints": [
            "POST /api/dd-vm/pull-tagjs",
            "POST /api/dd-vm/isolate-dispatcher",
            "POST /api/dd-vm/map-opcodes",
            "GET /api/dd-vm/state",
            "GET /api/dd-vm/opcodes",
            "POST /api/dd-vm/execute",
            "POST /api/dd-vm/emit",
            "POST /api/dd-vm/self-test",
        ],
        "warning": ("Este módulo es scaffold — DataDome rota opcodes ~30 días. "
                    "Mantener mapping actualizado o usar fallback browser completo."),
    }


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2))
