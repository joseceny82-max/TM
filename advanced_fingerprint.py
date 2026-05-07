"""advanced_fingerprint.py — WebGPU spoof + TLS ECH + advanced fingerprinting 2026.

Aborda 2 gaps identificados en RESEARCH_REPORT.md:
1. WebGPU spoofing (nueva señal alta entropía)
2. TLS 1.3 ECH (RFC 9849 marzo 2026) — preparado

API:
  generate_webgpu_profile(seed?) → config consistente
  get_webgpu_spoof_script(profile) → JS para inyectar en page
  get_ech_config_base64(hostname) → ECH config
  generate_full_fingerprint(seed) → fingerprint completo coherente
  validate_fingerprint_coherence(fp) → detecta inconsistencias
"""
import base64
import hashlib
import random
from datetime import datetime
from typing import Optional, Dict

# GPU profiles realistas 2026
GPU_PROFILES = [
    {"vendor": "NVIDIA Corporation",
     "renderer": "NVIDIA GeForce RTX 4080/PCIe/SSE2",
     "webgpu_vendor": "NVIDIA", "webgpu_arch": "ada",
     "max_buffer_size": 2147483648, "max_texture_size": 32768},
    {"vendor": "NVIDIA Corporation",
     "renderer": "NVIDIA GeForce RTX 4070/PCIe/SSE2",
     "webgpu_vendor": "NVIDIA", "webgpu_arch": "ada",
     "max_buffer_size": 1073741824, "max_texture_size": 32768},
    {"vendor": "AMD",
     "renderer": "AMD Radeon RX 7800 XT",
     "webgpu_vendor": "AMD", "webgpu_arch": "rdna3",
     "max_buffer_size": 2147483648, "max_texture_size": 32768},
    {"vendor": "Intel Inc.",
     "renderer": "Intel(R) Iris(R) Xe Graphics",
     "webgpu_vendor": "Intel", "webgpu_arch": "xe",
     "max_buffer_size": 268435456, "max_texture_size": 16384},
    {"vendor": "Apple Inc.",
     "renderer": "Apple M2 Pro",
     "webgpu_vendor": "Apple", "webgpu_arch": "apple-m2",
     "max_buffer_size": 4294967296, "max_texture_size": 16384},
    {"vendor": "ARM",
     "renderer": "Mali-G78 MC14",
     "webgpu_vendor": "ARM", "webgpu_arch": "mali",
     "max_buffer_size": 268435456, "max_texture_size": 8192},
]


def generate_webgpu_profile(seed: Optional[str] = None) -> Dict:
    """Genera un perfil WebGPU coherente.

    Args:
      seed: si se pasa, determinístico (mismo seed = mismo perfil)
    """
    rnd = random.Random(seed) if seed else random
    base = rnd.choice(GPU_PROFILES)

    # Features adaptativos (algunos GPUs soportan extensiones, otros no)
    features = ["depth-clip-control", "texture-compression-bc"]
    if base["webgpu_vendor"] in ("NVIDIA", "AMD", "Apple"):
        features.extend(["timestamp-query", "indirect-first-instance"])
    if base["webgpu_vendor"] == "Apple":
        features.extend(["texture-compression-astc"])
    if base["webgpu_vendor"] == "ARM":
        features = ["texture-compression-astc"]  # mobile sólo

    return {
        "adapter_info": {
            "vendor": base["webgpu_vendor"],
            "architecture": base["webgpu_arch"],
            "device": "",
            "description": base["renderer"],
        },
        "limits": {
            "maxBufferSize": base["max_buffer_size"],
            "maxTextureDimension2D": base["max_texture_size"],
            "maxTextureDimension3D": min(2048, base["max_texture_size"] // 16),
            "maxTextureArrayLayers": 256,
            "maxBindGroups": 4,
            "maxBindingsPerBindGroup": 1000,
            "maxDynamicUniformBuffersPerPipelineLayout": 8,
            "maxSampledTexturesPerShaderStage": 16,
            "maxSamplersPerShaderStage": 16,
            "maxStorageBuffersPerShaderStage": 8,
            "maxStorageTexturesPerShaderStage": 4,
            "maxUniformBuffersPerShaderStage": 12,
            "maxUniformBufferBindingSize": 65536,
            "maxStorageBufferBindingSize": base["max_buffer_size"] // 2,
            "maxVertexBuffers": 8,
            "maxVertexAttributes": 16,
            "maxVertexBufferArrayStride": 2048,
            "maxInterStageShaderComponents": 60,
            "maxComputeWorkgroupStorageSize": 16384,
            "maxComputeInvocationsPerWorkgroup": 256,
            "maxComputeWorkgroupSizeX": 256,
            "maxComputeWorkgroupSizeY": 256,
            "maxComputeWorkgroupSizeZ": 64,
            "maxComputeWorkgroupsPerDimension": 65535,
        },
        "features": features,
        "legacy_webgl": {
            "vendor": base["vendor"],
            "renderer": base["renderer"],
        },
    }


def get_webgpu_spoof_script(profile: Dict) -> str:
    """Genera JS que override navigator.gpu para devolver el perfil custom.

    Inyectable vía page.add_init_script() en Playwright.
    """
    import json
    adapter = json.dumps(profile.get("adapter_info", {}))
    limits = json.dumps(profile.get("limits", {}))
    features = json.dumps(profile.get("features", []))
    webgl = profile.get("legacy_webgl", {})

    return f"""
(function() {{
  // WebGPU Adapter spoof
  if (navigator.gpu) {{
    const originalRequestAdapter = navigator.gpu.requestAdapter;
    navigator.gpu.requestAdapter = async function(options) {{
      const adapter = await originalRequestAdapter.call(this, options);
      if (!adapter) return adapter;

      // Override adapter.info
      const infoProxy = {adapter};
      Object.defineProperty(adapter, 'info', {{
        get: () => infoProxy,
        configurable: true,
      }});

      // Override adapter.limits
      const limitsProxy = {limits};
      Object.defineProperty(adapter, 'limits', {{
        get: () => limitsProxy,
        configurable: true,
      }});

      // Override adapter.features
      const featuresSet = new Set({features});
      Object.defineProperty(adapter, 'features', {{
        get: () => featuresSet,
        configurable: true,
      }});

      return adapter;
    }};
  }}

  // WebGL legacy spoof (coherente con WebGPU)
  const webglVendor = {json.dumps(webgl.get('vendor', ''))};
  const webglRenderer = {json.dumps(webgl.get('renderer', ''))};
  const originalGetParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(parameter) {{
    // UNMASKED_VENDOR_WEBGL = 37445, UNMASKED_RENDERER_WEBGL = 37446
    if (parameter === 37445) return webglVendor;
    if (parameter === 37446) return webglRenderer;
    return originalGetParameter.call(this, parameter);
  }};
  if (typeof WebGL2RenderingContext !== 'undefined') {{
    const orig2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(p) {{
      if (p === 37445) return webglVendor;
      if (p === 37446) return webglRenderer;
      return orig2.call(this, p);
    }};
  }}
}})();
"""


def get_ech_config_base64(hostname: str,
                            public_name: Optional[str] = None) -> Dict:
    """Genera ECH (Encrypted Client Hello) config Base64 para TLS 1.3 RFC 9849.

    Nota: uso real requiere curl_cffi >=0.15 o nginx con --with-ech.
    Aquí generamos la struct y devolvemos base64 para inyectarla.
    """
    # Estructura ECHConfig (RFC 9849 secc. 4):
    # ECHConfig = {
    #   version: 0xfe0d (draft-13 / final v1.0),
    #   length: uint16,
    #   key_config: HpkeKeyConfig,
    #   maximum_name_length: uint8,
    #   public_name: opaque<1..255>,
    #   extensions: Extension<>,
    # }

    # Generamos una cifra fake (en producción la da el server via DNS HTTPS records)
    import secrets
    fake_key_id = secrets.token_bytes(1)
    fake_public_key = secrets.token_bytes(32)  # X25519
    public_name_bytes = (public_name or hostname).encode()

    # Build binary
    ech_binary = bytearray()
    ech_binary.extend(b"\xfe\x0d")                # version
    ech_binary.extend(fake_key_id)                 # key_id
    ech_binary.extend(b"\x00\x20")                 # kem_id (X25519)
    ech_binary.extend(b"\x00\x20")                 # public_key length
    ech_binary.extend(fake_public_key)
    # cipher_suites: HKDF-SHA256 + AES-128-GCM
    ech_binary.extend(b"\x00\x04\x00\x01\x00\x01")
    ech_binary.extend(bytes([64]))                 # maximum_name_length
    ech_binary.extend(bytes([len(public_name_bytes)]))
    ech_binary.extend(public_name_bytes)
    ech_binary.extend(b"\x00\x00")                 # extensions (empty)

    config_b64 = base64.b64encode(bytes(ech_binary)).decode()

    return {
        "hostname": hostname,
        "public_name": public_name or hostname,
        "ech_config_base64": config_b64,
        "ech_config_bytes": len(ech_binary),
        "version": "0xfe0d (v1.0 final)",
        "rfc": "RFC 9849 (March 2026)",
        "note": "Generada pseudo-fake. En producción usar DNS HTTPS records del servidor real.",
        "curl_cffi_integration": "requires curl_cffi>=0.15 + dns-over-https lookup",
    }


def generate_full_fingerprint(seed: Optional[str] = None,
                                 ua: Optional[str] = None) -> Dict:
    """Genera fingerprint COMPLETO y coherente: WebGPU + WebGL + timezone + locale.

    Todos los componentes se derivan del mismo seed → determinístico por sesión.
    """
    rnd = random.Random(seed) if seed else random
    webgpu = generate_webgpu_profile(seed=seed)

    # Coherencia: si GPU es Apple → platform MacIntel, TZ likely US
    webgl_vendor = webgpu["legacy_webgl"]["vendor"]
    if "Apple" in webgl_vendor:
        platform, tz = "MacIntel", rnd.choice(["America/New_York", "America/Los_Angeles"])
    elif "ARM" in webgl_vendor:
        platform, tz = rnd.choice(["Linux armv8l", "iPhone"]), "America/New_York"
    else:
        platform = "Win32"
        tz = rnd.choice(["America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Madrid"])

    # hw concurrency per GPU family
    if "Apple" in webgl_vendor:
        cores = rnd.choice([8, 10, 12, 16])
    elif "ARM" in webgl_vendor:
        cores = rnd.choice([6, 8])
    else:
        cores = rnd.choice([4, 8, 12, 16, 24])

    return {
        "seed": seed,
        "user_agent": ua or f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "platform": platform,
        "timezone": tz,
        "language": rnd.choice(["en-US", "en-GB", "es-ES", "fr-FR", "de-DE"]),
        "hardware_concurrency": cores,
        "device_memory": rnd.choice([4, 8, 16, 32]) if "Apple" not in webgl_vendor else rnd.choice([16, 32]),
        "screen": {
            "width": rnd.choice([1920, 2560, 3440, 1680]),
            "height": rnd.choice([1080, 1440, 1600, 2160]),
            "colorDepth": 24,
            "pixelRatio": rnd.choice([1.0, 1.25, 1.5, 2.0]),
        },
        "webgpu": webgpu,
        "webgl": webgpu["legacy_webgl"],
        "canvas_noise_seed": hashlib.md5((seed or "random").encode()).hexdigest()[:16],
        "audio_noise_seed": hashlib.md5(((seed or "") + "audio").encode()).hexdigest()[:16],
        "generated_at": datetime.now().isoformat(),
    }


def validate_fingerprint_coherence(fp: Dict) -> Dict:
    """Detecta inconsistencias en un fingerprint (bot signals típicos)."""
    issues = []

    platform = fp.get("platform", "")
    webgl_vendor = fp.get("webgl", {}).get("vendor", "")
    ua = fp.get("user_agent", "")

    # Platform vs UA
    if "Win" in ua and "Linux" in platform:
        issues.append("UA says Windows but platform is Linux")
    if "Mac" in ua and "Win32" in platform:
        issues.append("UA says Mac but platform is Win32")

    # WebGL vs Platform
    if "MacIntel" in platform and "NVIDIA" in webgl_vendor and "Apple" not in webgl_vendor:
        issues.append("Mac with NVIDIA (very rare, Apple usually)")
    if "ARM" in webgl_vendor and "Win32" in platform:
        issues.append("ARM GPU on Win32 (inconsistent)")

    # HW concurrency
    cores = fp.get("hardware_concurrency", 0)
    if cores == 0 or cores > 128:
        issues.append(f"hardware_concurrency {cores} out of realistic range")

    # Device memory
    mem = fp.get("device_memory", 0)
    if mem not in (2, 4, 8, 16, 32, 64):
        issues.append(f"device_memory {mem} not power-of-2 (bot signal)")

    # Screen
    sw = fp.get("screen", {}).get("width", 0)
    sh = fp.get("screen", {}).get("height", 0)
    if sw <= 0 or sh <= 0:
        issues.append("screen dims invalid")

    return {
        "ok": True,
        "coherent": len(issues) == 0,
        "issues": issues,
        "issue_count": len(issues),
        "score": max(0, 100 - len(issues) * 20),
    }


def self_test() -> dict:
    results = {"ok": True, "checks": {}}

    # WebGPU profile
    p1 = generate_webgpu_profile()
    results["checks"]["webgpu_profile_generated"] = "adapter_info" in p1
    results["checks"]["webgpu_limits_count"] = len(p1.get("limits", {}))
    results["checks"]["webgpu_features_count"] = len(p1.get("features", []))

    # Deterministic
    p2 = generate_webgpu_profile(seed="test_seed_123")
    p3 = generate_webgpu_profile(seed="test_seed_123")
    results["checks"]["webgpu_deterministic"] = (
        p2["adapter_info"]["description"] == p3["adapter_info"]["description"]
    )

    # Varies
    p4 = generate_webgpu_profile(seed="other_seed_xyz")
    results["checks"]["webgpu_varies_between_seeds"] = (
        p2["adapter_info"]["description"] != p4["adapter_info"]["description"]
    )

    # Spoof script
    script = get_webgpu_spoof_script(p1)
    results["checks"]["spoof_script_has_gpu"] = "navigator.gpu" in script
    results["checks"]["spoof_script_has_webgl"] = "getParameter" in script

    # ECH
    ech = get_ech_config_base64("www.ticketmaster.com")
    results["checks"]["ech_config_generated"] = "ech_config_base64" in ech
    results["checks"]["ech_version"] = ech.get("version")

    # Full fingerprint coherent
    fp = generate_full_fingerprint(seed="coherence_test")
    val = validate_fingerprint_coherence(fp)
    results["checks"]["full_fp_coherent"] = val["coherent"]
    results["checks"]["full_fp_score"] = val["score"]

    return results


def get_info() -> dict:
    return {
        "module": "advanced_fingerprint",
        "gpu_profiles_count": len(GPU_PROFILES),
        "capabilities": [
            "webgpu_spoof", "webgl_legacy_spoof",
            "tls_ech_config", "coherent_fingerprint",
            "fingerprint_validation",
        ],
    }
