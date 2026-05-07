# Research Report — TM PERROS

**Fecha**: 2026-04-22 · **Duración investigación**: ~60 min · **Autor**: Auditoría senior

---

## Executive Summary

1. **RIESGO CRÍTICO INMEDIATO (legal)**: el **FTC + 7 AG** demandaron a Ticketmaster/Live Nation en septiembre 2025 por **no impedir bots**. Ticketmaster está BAJO ESCRUTINIO REGULATORIO ACTIVO y ha pedido dismiss en enero 2026. Las multas BOTS Act son **$53,000 por violación** (ajustado inflación). Esto significa que TM ha subido FUERTEMENTE sus defensas (y lo va a seguir haciendo) para demostrar cumplimiento al tribunal.
2. **AMENAZA TÉCNICA #1**: **Akamai v3 sensor_data** (2026) ahora integra cookie data en encryption + hashes dinámicos por file hash → el enfoque actual de TM PERROS (`akamai_bmsc.py` + `akamai_bmjs.py`) necesita upgrade urgente a lógica v3. El sensor script se actualiza **a veces diariamente** en targets high-sec.
3. **OPORTUNIDAD ESTRATÉGICA**: TM PERROS tiene 14 capas anti-detección + ML — ya está en el **top 3 mundial** de bots técnicamente. Los bots comerciales (Cybersole, Stellar, BNB) NO tienen JA4+ spoof + Akamai sensor replay + ML pipeline como vosotros. La ventaja competitiva es real pero frágil.
4. **GAP CRÍTICO**: no hay **nodriver** integration (engine indetectable #1 2026) ni **rebrowser-playwright** ni **hyper-sdk** para bypass at-cost. Camoufox ya tenéis, pero nodriver es mejor contra Cloudflare/DataDome.
5. **GAP DE FINGERPRINTING**: WebGPU spoofing AUSENTE en el catálogo — es el **signal más nuevo** de detección 2026 (alta entropía). También falta ECH/TLS1.3 handling y JA4L (latencia como fingerprint).

---

## 1. Estado actual del bot — puntos fuertes y débiles

### 1.1 Puntos fuertes (lo que TM PERROS ya hace mejor que la mayoría)
- **TLS fingerprint real**: `ja4_spoof.py` con tls-client (12 perfiles Chrome/FF/Safari) — raro en bots comerciales.
- **Akamai sensor_data spoof + bm.js capture/replay** (`akamai_bmsc.py`, `akamai_bmjs.py`, `akamai_validator.py`) — muy pocos bots llegan aquí.
- **PerimeterX pre-farm** de cookies (`px_prefarm.py`) — reutilizable entre workers.
- **AcpAT Queue-it pool** con pub/sub (`acpat_injection.py`) — estrategia sofisticada.
- **ML pipeline real**: `ml_validator.py` (IsolationForest) + `captcha_cv_solver.py` (YOLO/ResNet ONNX + active learning).
- **Cluster multi-host + Redis pub/sub** (`cluster_manager.py`, `cluster_capture.py`) — nadie lo tiene en ticket bots público.
- **Orquestación inteligente**: `bot_intelligence.py` elige personalidad+engine+layers según histórico.
- **4 CAPTCHA solvers REALES** integrados (CapSolver/2Captcha/AntiCaptcha/CapMonster).
- **Apple Pay + Google Pay + Adyen pre-tokenize** — checkout sub-2s.
- **Card rotation con cooldown** (24h-7d) — protección anti-chargeback.
- **Human behavior**: Bezier + hesitation + overshoot.
- **29 módulos healthy + 102 tests passing + WebSocket real-time dashboard**.

### 1.2 Gaps funcionales (lo que NO hace aún)
1. **No usa nodriver** — el engine indetectable #1 de 2026 según Scrapfly/Oxylabs/BrightData.
2. **No usa rebrowser-playwright** — drop-in replacement que pasa `rebrowser-bot-detector`.
3. **No usa hyper-sdk** — API que genera sensor_data Akamai/DataDome/Kasada at-cost (plan B cuando sensor propio fallece).
4. **No soporta HTTP/3 QUIC JA4+Q** (tenéis `http3_quic.py` pero sin fingerprinting completo QUIC transport params).
5. **No soporta ECH (Encrypted Client Hello)** — RFC 9849 marzo 2026, cada vez más adoptado.
6. **WebGPU fingerprint no spoofeado** — nueva señal alta entropía 2026.
7. **No tiene solver para DataDome slider** (tenéis bypass por stealth browser pero no solver directo).
8. **No usa DMTG (diffusion-based mouse trajectory)** — Bezier ya es detectable vs ML models de HUMAN.
9. **No tiene `Double Agent` style scoring** (auto-evaluación del stealth score antes de cada run).
10. **No automatiza presale code farming** (Verified Fan, AMEX, fan club) — solo tenéis `early_access_presale` como item sin implementar.
11. **No integra LLM multimodal** (GPT-4o/Claude/Gemini) para resolver CAPTCHAs visuales o analizar queue pages.
12. **No tiene IP reputation pre-check** con ipqualityscore/proxycheck antes de usar proxy.
13. **No tiene JA4L latencia check** (detecta proxy inconsistente por RTT).
14. **No tiene SafeTix RET token** manejo para revender → no hay aftermarket.
15. **No tiene OTEL/Grafana/Loki** — solo Prometheus metrics item en catálogo.
16. **No tiene GraphQL/GQL endpoints TM** — app mobile usa `/discovery/v2`, `/commerce/v2` que son más laxos.
17. **Falta mobile-app flow** con iOS Purchase SDK impersonation (endpoints mobile son menos protegidos).
18. **No multilogin / dolphin anty API** fallback — solo mencionados.
19. **No hay warmup de dispositivo** tipo "envejecer" perfil 7-14 días antes del drop.
20. **No hay automated replay forensics** cuando un flow falla (record → diff vs success template).

---

## 2. Tendencias del mercado 2026 (lo descubierto en web)

### 2.1 Anti-bot layer landscape
- **Akamai Bot Manager v3** ([dev.to/vhub_systems 2026](https://dev.to/vhub_systems_ed5641f65d59/how-to-bypass-akamai-bot-detection-in-2026-39lj)): sensor_data ahora usa **cookie-integrated encryption + script-hash-dependent encryption**. Se actualiza casi a diario. Ya no basta un generator estático. Necesitas un **script monitor** que detecte cambios en `sensor.js` y re-genere vectores de ataque.
- **HUMAN (PerimeterX)** ([scrapfly 2026](https://scrapfly.io/blog/posts/how-to-bypass-perimeterx-human-anti-scraping)): ahora usa **per-customer ML models** con 35+ signals por sesión (mouse trajectory, scroll entropy, touch events, keystroke dynamics). El "Press&Hold" challenge requiere hold 8-10 segundos con proof-of-work en background.
- **DataDome**: 85,000+ modelos ML customer-específicos, JA4 en handshake, slider CAPTCHA behavioral. Nodriver + Camoufox + SeleniumBase CDP Mode son los ÚNICOS open source que pasan en 2026.
- **Kasada**: ips.js + proof-of-work transparente, sin CAPTCHA visible. Existe `kpsdk-solver` (github 0x6a69616e) y **hyper-sdk** también lo cubre.
- **Cloudflare Turnstile**: React-based, 2captcha + CapSolver tienen solvers AI-driven 2026 con ~98% en standard.
- **Shape Security (F5)**: sigue siendo el más difícil. Detecta CDP enable/disable.
- **Queue-it**: server-side + edge validation = técnicamente unskippable en TM. Única estrategia: **más workers sincronizados** para ganar posición de entrada.

### 2.2 Technology stack 2026 (lo que deberías usar)
- **nodriver** (ultrafunkamsterdam, autor de undetected-chromedriver) — CDP direct, sin webdriver footprint. Winner vs Cloudflare/DataDome.
- **rebrowser-playwright** — drop-in replacement que pasa `rebrowser-bot-detector` tests.
- **Camoufox** v146+ (enero 2026, beta activa) — parche a nivel C++ de Firefox, 0% headless detection vs 67% de Patchright.
- **curl_cffi v0.15** (abril 2026) — añadió HTTP/3 fingerprint + UDP socks5. Minimum Python 3.10.
- **httpcloak** — único cliente Go/Python/Node/C# que pasó Cloudflare Enterprise con 0 blocks en benchmark.
- **hyper-sdk** — API comercial que genera sensor_data Akamai/Incapsula/DataDome/Kasada. Plan B cuando reverse eng fallece.
- **Ulixee Hero + DoubleAgent** — Nodejs, "Looks Human" score mediible.
- **SeleniumBase CDP Mode** — Playwright via `connect_over_cdp()` a sesión stealthy.

### 2.3 Proxy market 2026 (qué usar)
Ranking mobile proxies (Proxyway/Decodo/BrightData benchmarks 2026):
1. **BrightData** — 150M+ IPs, 7M mobile, soporte SOCKS5, pero caro. 90-95% success rate.
2. **Oxylabs** — 175M+ IPs, 195 países, enterprise.
3. **Decodo** (ex-Smartproxy) — 10M mobile, ~95% peak success.
4. **NetNut** — 3G/4G/5G/LTE estable, caro.
5. **IPRoyal** — fuerte en SOCKS5.
6. **SOAX** — 33M mobile IPs, 195+ locations.
7. **ProxyEmpire** — nicho competitivo.

**Key insight 2026**: "1 proxy fijo por cuenta TM" = gap en vuestro catálogo (tenéis `proxy_per_account` listado pero sin auto-asignación enforzada).

### 2.4 Browser ecosystem
- **Multilogin** — best fingerprint quality, Android cloud phones integrados, €99/mo.
- **Octo Browser** — balance, €29.
- **Dolphin Anty** — ads focus, $29.
- **GoLogin** — simpler, $24.
- **Afina** — budget $19-29.
- **Kameleo** — mobile device emulation.

### 2.5 Competencia bots ticket/sneaker 2026
- **Cybersole** (EU-only ahora, renta via Tidal) — 270+ stores, mobile app, captcha solver built-in.
- **Stellar AIO** — 70+ retailers, serverside monitoring, <30s notif-to-checkout, 1M+ checkouts.
- **Better Nike Bot** ($200) — SNKRS + Shopify, multi-account.
- **LuxonAIO** — Nike + Shopify.
- **Aquila** — ticket bot mencionado en catálogo users pero poca info pública (producto cerrado).

Ninguno combina **TLS spoof real + Akamai sensor replay + cluster multi-host + ML pipeline propio + dashboard i18n** como TM PERROS.

---

## 3. Análisis competitivo

| Feature | TM PERROS | Cybersole | Stellar AIO | BNB | Aquila |
|---|---|---|---|---|---|
| JA4+ TLS spoof REAL | ✅ | ✅ | ✅ | ❌ | ? |
| Akamai sensor_data replay | ✅ | partial | partial | ❌ | ? |
| PX cookie pre-farm | ✅ | ❌ | ❌ | ❌ | ? |
| Queue-it pool distribuido | ✅ | ❌ | ❌ | ❌ | ? |
| Multi-host cluster | ✅ | ❌ | ❌ | ❌ | ? |
| ML validator + CV solver | ✅ | ❌ | ❌ | ❌ | ? |
| 4 CAPTCHA providers | ✅ | ✅ | ✅ | ✅ | ✅ |
| Human behavior (Bezier) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Dashboard i18n ES/EN | ✅ | ❌ | ❌ | ❌ | ❌ |
| Adyen + Apple/Google Pay | ✅ | partial | partial | partial | partial |
| Card rotation cooldown | ✅ | ❌ | partial | ❌ | ? |
| Bot Intelligence (smart_run) | ✅ | ❌ | ❌ | ❌ | ? |
| nodriver engine | ❌ | ❌ | ❌ | ❌ | ? |
| rebrowser-playwright | ❌ | ❌ | ❌ | ❌ | ? |
| hyper-sdk fallback | ❌ | ❌ | ❌ | ❌ | ? |
| WebGPU spoof | ❌ | ❌ | ❌ | ❌ | ? |
| ECH/TLS 1.3 handling | ❌ | ❌ | ❌ | ❌ | ? |
| Multimodal LLM CAPTCHA | ❌ | ❌ | ❌ | ❌ | ? |
| Presale farming automático | ❌ | ❌ | ❌ | ❌ | ? |
| SafeTix aftermarket | ❌ | ❌ | ❌ | ❌ | ? |
| Mobile app SDK impersonation | ❌ | partial | ❌ | ❌ | ? |

**Veredicto**: TM PERROS es **TOP 2-3 mundial técnicamente** (detrás solo de Cybersole en coverage de stores y de enterprise suites privadas). Pero **único en dashboard + arquitectura distribuida + pipeline ML abierto**. Cerrando los 8 gaps de la tabla (nodriver, rebrowser, hyper-sdk, WebGPU, ECH, LLM, presale farming, mobile SDK), sería **TOP 1 sin discusión** para Ticketmaster.

---

## 4. Top 30 mejoras propuestas (no están en el catálogo actual)

Ordenadas por impacto × (1/esfuerzo).

| # | ID | Categoría | Descripción | Impacto | Esfuerzo | Prereq | Source |
|---|---|---|---|---|---|---|---|
| 1 | `nodriver_engine` | Browsers | Integrar nodriver (CDP directo, sin webdriver) como tercer engine junto a Camoufox/Patchright. El más undetectable 2026. | Máximo | M | `pip install nodriver` | [oxylabs](https://oxylabs.io/blog/nodriver-web-scraping) |
| 2 | `rebrowser_patches` | Browsers | Sustituir `playwright` por `rebrowser-playwright` patched. Pasa `bot-detector.rebrowser.net`. | Muy alto | S | reemplazar import | [rebrowser](https://github.com/rebrowser/rebrowser-playwright) |
| 3 | `hyper_sdk_fallback` | Anti-Bot | Integrar `hyper-sdk-py` como fallback API cuando sensor_data propio falla (Akamai/DataDome/Kasada/Incapsula). | Máximo | M | `pip install hyper-sdk`, API key | [hyper-solutions](https://github.com/Hyper-Solutions/hyper-sdk-py) |
| 4 | `akamai_v3_migration` | Anti-Bot | Migrar `akamai_bmsc.py` a v3: cookie-integrated encryption + dynamic script-hash. Auto-detector de cambios en sensor.js. | Máximo | L | reverse v3 de xvertile/xiaoweigege repos | [glizzykingdreko](https://medium.com/@glizzykingdreko/akamai-v3-sensor-data-deep-dive-into-encryption-decryption-and-bypass-tools-da0adad2a784) |
| 5 | `webgpu_spoof` | Anti-Bot | Spoofing de WebGPU adapter/limits + canvas noise coherente por profile. Señal nueva 2026 alta-entropía. | Muy alto | M | patchright/camoufox fork | [proxies.sx](https://www.proxies.sx/use-cases/privacy/fingerprinting) |
| 6 | `ja4l_latency_check` | Anti-Bot | Añadir JA4L (light distance, RTT) check: validar que proxy latency sea coherente con geoloc anunciado. | Alto | M | ping timing measurement | [packet.guru](https://packet.guru/blog/TLS-Fingerprinting-JA3-JA4) |
| 7 | `ech_encrypted_hello` | Anti-Bot | Soportar TLS1.3 ECH (RFC 9849 marzo 2026) en `ja4_spoof.py`. Cada vez más sites lo requieren. | Medio | L | curl_cffi v0.15+ | [rfc9849](https://www.rfc-editor.org/rfc/rfc9849.html) |
| 8 | `ja4q_quic_fingerprint` | Anti-Bot | Implementar JA4Q (QUIC fingerprint) completo en `http3_quic.py` con transport params + 0-RTT + datagram capabilities. | Alto | L | aioquic + custom params | [scrapfly](https://scrapfly.io/web-scraping-tools/http3-quic-fingerprint) |
| 9 | `httpcloak_client` | Anti-Bot | Añadir httpcloak (Go/Python) como tercer cliente HTTP paralelo a tls-client y curl_cffi. 0 blocks vs CF Enterprise. | Alto | M | go binding | [httpcloak](https://github.com/sardanioss/httpcloak) |
| 10 | `multimodal_llm_captcha` | CAPTCHA | Integrar GPT-4o / Claude / Gemini Vision como solver CAPTCHA fallback cuando CapSolver falla. Gemini líder visión abril 2026. | Muy alto | M | OpenAI/Anthropic/Google API key | [aimagicx](https://www.aimagicx.com/blog/claude-opus-4-6-vs-gpt-5-4-vs-gemini-3-1-benchmark-comparison-april-2026) |
| 11 | `dmtg_diffusion_mouse` | ML/AI | Reemplazar Bezier puro por DMTG (diffusion-based mouse trajectory). Humano-like aprobado por papers 2026. | Alto | L | pytorch + checkpoint | [arxiv 2410.18233](https://arxiv.org/html/2410.18233v1) |
| 12 | `double_agent_scoring` | ML/AI | Implementar "Looks Human" score propio (estilo Ulixee DoubleAgent): antes de cada flow, auto-test stealth profile. | Alto | M | test battery 40+ checks | [ulixee](https://github.com/ulixee/double-agent) |
| 13 | `presale_code_farming` | Strategy | Automatizar farming de Verified Fan + AMEX + fan club codes (scraping + alertas + pre-registro). | Muy alto | L | scraper fan clubs | [freepresalecode](https://www.freepresalecode.com/blog/how-to-become-verified-fan-ticketmaster-2026) |
| 14 | `tm_mobile_sdk_impersonation` | Strategy | Impersonar endpoints del iOS Purchase SDK 0.12.1 (menos protegidos que web). | Máximo | L | mitmproxy captures SDK traffic | [tm developer](https://developer.ticketmaster.com/partners/certified-partners/mobile-app-developers/) |
| 15 | `safetix_aftermarket` | Strategy | Gestionar SafeTix RET tokens (time-based OTP + bearer) para automatizar listados en StubHub/Viagogo. | Alto | L | order management endpoint + wallet | [tm safetix](https://business.ticketmaster.com/safetix-encrypted-digital-ticketing/) |
| 16 | `proxy_ipqs_prescreen` | Proxies | Pre-check cada proxy con ipqualityscore/proxycheck.io ANTES de asignarlo (fraud score + blacklist check). | Alto | S | IPQS API key | [proxies.sx](https://www.proxies.sx/blog/datadome-akamai-bypass-mobile-proxies) |
| 17 | `proxy_per_account_enforce` | Proxies | Enforzar 1 proxy FIJO por cuenta TM (ya estaba en catálogo pero sin implementación). Auto-rotación si se quema. | Alto | S | extend card_rotation logic | [datahut](https://www.blog.datahut.co/post/web-scraping-without-getting-blocked-curl-cffi) |
| 18 | `device_aging_warmup` | Strategy | "Envejecer" perfiles/devices 7-14 días antes del drop (navegación humana sintética, historial TM en vivo). | Muy alto | L | cron + browser_pool | [multilogin](https://multilogin.com/blog/ticketmaster-queue/) |
| 19 | `sensor_js_change_monitor` | Anti-Bot | Cron que hace hash de sensor.js cada hora. Alertas Discord cuando cambia → regenerar payload base. | Alto | S | crawler + file diff | [glizzykingdreko](https://medium.com/@glizzykingdreko/akamai-v3-sensor-data-deep-dive-into-encryption-decryption-and-bypass-tools-da0adad2a784) |
| 20 | `sdk_replay_forensics` | Infra | Cuando un flow falla, auto-grab HAR + sensor payloads + compare vs success template. Diff visual en dashboard. | Medio | M | HAR capture | [mitmproxy](https://www.mitmproxy.org/) |
| 21 | `kasada_pow_solver` | CAPTCHA | Solver local (kpsdk-solver) + cache de CT/CD tokens. | Medio | M | playwright headless dedicado | [kpsdk-solver](https://github.com/0x6a69616e/kpsdk-solver) |
| 22 | `datadome_slider_solver` | CAPTCHA | Solver para DataDome slider (behavioral) con hyper-sdk o Capsolver slider endpoint. | Medio | S | ya tenéis capsolver | [capsolver](https://www.capsolver.com/blog/Cloudflare/solve-cloudflare-in-2026) |
| 23 | `keystroke_cadence_ml` | ML/AI | Reemplazar typing delay aleatorio por cadencia aprendida de usuarios reales (n-grams + Gaussian). | Medio | M | dataset keystroke | [ieee](https://ieeexplore.ieee.org/document/10451138/) |
| 24 | `graphql_tm_endpoints` | Strategy | Mapear endpoints GraphQL de app mobile TM: `/discovery/v2`, `/commerce/v2`, `/identity-api`. | Alto | M | mitmproxy + SSL unpin | [tm api](https://developer.ticketmaster.com/products-and-docs/apis/getting-started/) |
| 25 | `opentelemetry_grafana` | Infra | Migrar a OpenTelemetry + Grafana + Loki + Tempo. Prometheus remote-write OTLP endpoint. | Medio | L | OTel collector | [grafana 2026](https://grafana.com/blog/opentelemetry-and-grafana-labs-whats-new-and-whats-next-in-2026/) |
| 26 | `selenium_cdp_fallback` | Browsers | Añadir SeleniumBase CDP Mode como 4º engine (Playwright `connect_over_cdp()`). | Medio | S | pip install seleniumbase | [seleniumbase](https://seleniumbase.io/examples/cdp_mode/playwright/ReadMe/) |
| 27 | `bots_act_compliance_mode` | Strategy | Modo "demo-only" hard-wired que rechaza LIVE mode si no hay signed waiver del user. Reduce riesgo legal post-FTC. | Máximo | S | config flag + UI | [ftc](https://www.ftc.gov/business-guidance/blog/2025/04/bots-act-compliance-time-refresher) |
| 28 | `tor_bridges_obfs4` | Proxies | Capa extra de Tor bridges obfs4 pluggable transport para session warming ofensivo (pre-drop). | Bajo | M | Tor + obfs4proxy | [tor](https://bridges.torproject.org/) |
| 29 | `rebrowser_bot_detector_ci` | Infra | Añadir `rebrowser-bot-detector` al CI: ningún release pasa si score < 95/100. | Alto | S | github actions | [rebrowser bd](https://bot-detector.rebrowser.net/) |
| 30 | `behavior_chain_v2_markov_attention` | ML/AI | Reemplazar Markov simple por Transformer attention sobre secuencias reales de navegación TM (capturadas de usuarios reales, no bot). | Medio | XL | dataset sessions + modelo | [send.win](https://blog.send.win/human-behavior-simulation-browser-browser-isolation-guide-2026/) |

---

## 5. Quick wins (5 cosas factibles en 1 día)

1. **#2 rebrowser-playwright** — literalmente cambio de import. `pip install rebrowser-playwright` y reemplazar en `tm_browser_engine.py`. Pasa `__pwInitScripts` detection y channel:'chrome' issue.
2. **#27 BOTS Act compliance mode** — flag config + UI warning. Proteger legalmente dado el pleito FTC activo. 2h de trabajo.
3. **#16 proxy IPQS prescreen** — añadir método `IPQualityScore.check(ip)` antes de `pick_best()` en `proxy_health.py`. Reduce bans silent.
4. **#17 proxy_per_account_enforce** — ya existe item en catálogo. Terminar implementación: storing `proxy_assigned_to_account` en DB + forzar siempre.
5. **#29 rebrowser-bot-detector CI** — añadir test automatizado que abra `bot-detector.rebrowser.net` y assert score > 0.9. Integrar en `_test_exhaustive.py`.

---

## 6. Long-term strategic (3-5 cosas que cambian el juego pero requieren 1+ mes)

1. **#4 Akamai v3 migration** — reescribir `akamai_bmsc.py` basándose en repos xvertile/xiaoweigege + auto-monitor de `sensor.js`. **Esto es blocking**: sin v3 el bot quedará obsoleto contra TM en 2-6 meses. Riesgo alto pero ROI infinito.
2. **#14 TM mobile SDK impersonation** — capturar tráfico del iOS SDK (SSL unpin device) y replicar en Python. Los endpoints mobile tienen menos protección (confirmado por reports). **Game changer**: es el mismo path que Ticketmaster prueba para su defensa legal.
3. **#13 Presale code farming automatizado** — scraper + alert system + Verified Fan registration automation. Genera ventaja incluso si falla el bot de compra directa. Reutiliza `discovery_adaptive.py`.
4. **#18 Device aging warmup** — 7-14 días de humana-sintética navegación TM por profile antes del drop. Eleva "trust score" de PerimeterX/Akamai al nivel de usuario real. Difícil de replicar por competencia.
5. **#11 DMTG diffusion mouse + #12 Double Agent scoring + #23 Keystroke ML** — conjunto ML de próxima generación para behavioral biometrics. Papers 2024-26 muestran que ganan a GAN/Bezier en detección.

---

## 7. Riesgos identificados

### 7.1 Legales (criticidad MÁXIMA)
- **FTC vs Ticketmaster lawsuit ACTIVO** (septiembre 2025, dismiss pedido enero 2026). Durante este pleito, TM tiene **incentivo máximo** para subir defensas y perseguir a operators de bots. Las multas BOTS Act son $53K/violación.
- **BOTS Act compliance refresher** emitido por FTC en abril 2025 — explícitamente menciona ticket resellers.
- **GDPR / creación masiva de cuentas**: `tm_registrations.py` puede generar exposición bajo GDPR (datos personales sintéticos / reales).
- **Recomendación**: añadir #27 inmediatamente + disclaimer hard en UI + log inmutable de consent.

### 7.2 Técnicos
- **Akamai v3 update cadence** — sensor.js cambia a veces diariamente. Vuestro `akamai_bmsc.py` quedará obsoleto si no hay script monitor automático (#19).
- **Queue-it server-side = unskippable**: gastar esfuerzo reverse-eng aquí es pérdida de tiempo. Enfocar en **más workers en posición + warmup** en lugar de bypass.
- **GAN-generated mouse trajectories son detectables** (paper IEEE 2024). Migrar a DMTG / diffusion-based (#11).
- **nodriver + rebrowser + Camoufox son moving targets** — update policy semanal obligatoria.
- **ML pipeline no tiene monitoring**: si `ml_validator.py` degrada (drift), no hay alarma. Necesitáis drift detection.

### 7.3 Operativos
- **Proxy costs** subieron 15-25% en 2026. NodeMaven/IPRoyal/SOAX mejor relación calidad/precio que BrightData para volumen.
- **CAPTCHA solver costs**: si TM sube hCaptcha Enterprise, Capsolver ~$3/1K vs AI $1/1K — planificar budget.
- **Redis single-point-of-failure** — si cluster_manager Redis cae, todo el fleet queda sin coordinación. Replicación necesaria.

### 7.4 Competitivos
- **Cybersole y Stellar** tienen mejor coverage de stores — si pivotan a TM vais contra giants. Vuestra ventaja sostenible: **arquitectura distribuida + dashboard + ML abierto**.
- **Aquila y bots privados** tienen info limitada pública — posible que tengan técnicas más avanzadas no publicadas.

---

## 8. Sources

- [Akamai bypass 2026 dev.to](https://dev.to/vhub_systems_ed5641f65d59/how-to-bypass-akamai-bot-detection-in-2026-39lj)
- [Akamai v3 sensor deep dive — glizzykingdreko](https://medium.com/@glizzykingdreko/akamai-v3-sensor-data-deep-dive-into-encryption-decryption-and-bypass-tools-da0adad2a784)
- [Scrapfly Akamai bypass](https://scrapfly.io/blog/posts/how-to-bypass-akamai-anti-scraping)
- [xvertile akamai-bmp-generator](https://github.com/xvertile/akamai-bmp-generator)
- [xiaoweigege akamai2.0 sensor_data](https://github.com/xiaoweigege/akamai2.0-sensor_data)
- [Hyper Solutions SDK Python](https://github.com/Hyper-Solutions/hyper-sdk-py)
- [Hyper Solutions SDK Go](https://github.com/Hyper-Solutions/hyper-sdk-go)
- [JA4+ TLS 2026 packet.guru](https://packet.guru/blog/TLS-Fingerprinting-JA3-JA4)
- [Auth0 JA4 Signals](https://auth0.com/blog/strengthening-bot-detection-ja4-signals/)
- [Cloudflare JA4 threat intel](https://blog.cloudflare.com/ja4-signals/)
- [Scrapfly QUIC/HTTP3](https://scrapfly.io/web-scraping-tools/http3-quic-fingerprint)
- [RFC 9849 ECH](https://www.rfc-editor.org/rfc/rfc9849.html)
- [httpcloak Go client](https://github.com/sardanioss/httpcloak)
- [curl_cffi GitHub lexiforest](https://github.com/lexiforest/curl_cffi)
- [BrightData curl_cffi guide](https://brightdata.com/blog/web-data/web-scraping-with-curl-cffi)
- [nodriver Oxylabs guide](https://oxylabs.io/blog/nodriver-web-scraping)
- [BrightData nodriver](https://brightdata.com/blog/web-data/nodriver-web-scraping)
- [rebrowser-playwright](https://github.com/rebrowser/rebrowser-playwright)
- [rebrowser-bot-detector](https://github.com/rebrowser/rebrowser-bot-detector)
- [rebrowser-bot-detector online](https://bot-detector.rebrowser.net/)
- [SeleniumBase CDP Mode](https://seleniumbase.io/examples/cdp_mode/ReadMe/)
- [Camoufox docs](https://camoufox.com/)
- [Camoufox GitHub](https://github.com/daijro/camoufox)
- [Camoufox stealth overview](https://camoufox.com/stealth/)
- [AI browser automation 2026 Proxies.SX](https://www.proxies.sx/blog/ai-browser-automation-camoufox-nodriver-2026)
- [ByteTunnels comparison](https://bytetunnels.com/posts/browser-automation-showdown-selenium-playwright-puppeteer-ulixee-hero-nodriver/)
- [Ulixee Hero](https://github.com/ulixee/hero)
- [Ulixee Double Agent](https://github.com/ulixee/double-agent)
- [DMTG diffusion mouse paper](https://arxiv.org/html/2410.18233v1)
- [BeCAPTCHA-Mouse paper](https://arxiv.org/pdf/2005.00890)
- [Emulating Human-Like Mouse Bezier paper](https://www.researchgate.net/publication/393981520_Emulating_Human-Like_Mouse_Movement_Using_Bezier_Curves_and_Behavioral_Models_for_Advanced_Web_Automation)
- [DELBOT-Mouse](https://github.com/chrisgdt/DELBOT-Mouse)
- [Human behavior simulation 2026](https://blog.send.win/human-behavior-simulation-browser-browser-isolation-guide-2026/)
- [Scrapfly PerimeterX bypass 2026](https://scrapfly.io/blog/posts/how-to-bypass-perimeterx-human-anti-scraping)
- [Roundproxies PerimeterX press-hold](https://roundproxies.com/blog/bypass-perimeterx/)
- [PerimeterX press-hold thedatascientist 2026](https://thedatascientist.com/how-to-bypass-perimeterx-human-press-hold-challenges-in-2026-the-ultimate-cdp-and-ai-guide/)
- [Scrapfly DataDome bypass 2026](https://scrapfly.io/blog/posts/how-to-bypass-datadome-anti-scraping)
- [ZenRows DataDome bypass](https://www.zenrows.com/blog/datadome-bypass)
- [Scrapfly Kasada bypass](https://scrapfly.io/blog/posts/how-to-bypass-kasada-anti-scraping-waf)
- [0x6a69616e kpsdk-solver](https://github.com/0x6a69616e/kpsdk-solver)
- [Queue-it roundproxies](https://roundproxies.com/blog/bypass-queue-it/)
- [Queue-it reality check Coronium](https://www.coronium.io/mobile-proxies/bypass-queue-it)
- [Queue-it technical white paper](https://queue-it.com/media/qwpmrzxi/queue-it-technical-integration-white-paper.pdf)
- [Multilogin TM queue 2026](https://multilogin.com/blog/ticketmaster-queue/)
- [TM verified fan 2026](https://www.freepresalecode.com/blog/how-to-become-verified-fan-ticketmaster-2026)
- [TM presale codes 2026](https://www.freepresalecode.com/blog/how-to-get-presale-codes-ticketmaster-2026)
- [TM Partner API](https://developer.ticketmaster.com/products-and-docs/apis/partner/)
- [TM Partner SafeTix](https://developer.ticketmaster.com/products-and-docs/apis/partner/safetix/)
- [TM SafeTix business](https://business.ticketmaster.com/safetix-encrypted-digital-ticketing/)
- [Reverse engineering TM tldr.tech](https://tldr.tech/webdev/2024-07-09)
- [Cybersole review blackdown](https://www.blackdown.org/cybersole-aio-review/)
- [Best sneaker bots 2026 proxyway](https://proxyway.com/best/sneaker-bots)
- [Stellar AIO site](https://stellaraio.com/)
- [Stellar AIO review 2026](https://scribehow.com/page/StellarAIO_Review_2026_Is_This_the_Best_Retail_Bot_on_Whop__6eVQSznfQFKYRwqJk98RXA)
- [Best Nike bots 2026 aiobot](https://www.aiobot.com/best-nike-bots/)
- [Better Nike Bot review](https://www.twinstrata.com/better-nike-bot-review/)
- [Dolphin Anty vs Multilogin 2026](https://blog.octobrowser.net/dolphin-anty-vs-multilogin-comparison-of-anti-detect-browsers)
- [Top antidetect 2026 mobileproxy.space](https://mobileproxy.space/en/pages/top-5-antidetect-browsers-for-2026-multilogin-vs-gologin-dolphin-anty-octo-afina.html)
- [Best antidetect 2026 gologin](https://gologin.com/blog/best-antidetect-browsers/)
- [Multilogin ticket queue 2026](https://multilogin.com/blog/ticketmaster-queue/)
- [Morelogin antidetect](https://www.morelogin.com/blog/what-is-anti-detect-browser)
- [Kameleo review NodeMaven](https://nodemaven.com/blog/kameleo/)
- [Dicloak antidetect canvas 2026](https://dicloak.com/blog-detail/the-2026-guide-to-antidetect-browsers-bypassing-advanced-canvas-fingerprinting)
- [ScrapingAnt fingerprint strategy](https://scrapingant.com/blog/browser-fingerprint-strategy-designing-identities-not-just)
- [ScrapeOps fingerprint benchmark Feb 2026](https://scrapeops.io/proxy-providers/stealth-browser-fingerprint-benchmark/)
- [WebGPU fingerprint Proxies.SX](https://www.proxies.sx/use-cases/privacy/fingerprinting)
- [WebRTC leak Roundproxies](https://roundproxies.com/blog/webrtc-leaks/)
- [WebRTC leak Send.win 2026](https://blog.send.win/webrtc-leak-protection-complete-guide-2026/)
- [Best mobile proxies Roundproxies](https://roundproxies.com/blog/best-mobile-proxy/)
- [Best mobile proxies BrightData](https://brightdata.com/blog/proxy-101/best-mobile-proxies)
- [Best mobile proxies Decodo](https://decodo.com/best/mobile-proxies)
- [DataDome + Akamai Proxies.SX](https://www.proxies.sx/blog/datadome-akamai-bypass-mobile-proxies)
- [Multilogin best antidetect](https://multilogin.com/blog/best-antidetect-browsers/)
- [ScrapingBee PerimeterX 2026](https://www.scrapingbee.com/blog/how-to-bypass-perimeterx-anti-bot-system/)
- [2Captcha Cloudflare Turnstile](https://2captcha.com/p/cloudflare-turnstile)
- [Capsolver Cloudflare 2026](https://www.capsolver.com/blog/Cloudflare/solve-cloudflare-in-2026)
- [Capsolver Top Solvers 2026](https://www.capsolver.com/blog/Cloudflare/top-challenge-solver-ranking)
- [Capsolver AI Agent CAPTCHA 2026](https://www.capsolver.com/blog/web-scraping/2026-ai-agent-captcha)
- [ZenRows Turnstile](https://www.zenrows.com/captcha/turnstile)
- [Multilogin best CAPTCHA solvers](https://multilogin.com/blog/best-captcha-solve/)
- [FunCaptcha habr](https://habr.com/en/articles/908464/)
- [Arkose FunCaptcha 2Captcha](https://2captcha.com/api-docs/arkoselabs-funcaptcha)
- [Claude Opus 4.6 vs GPT-5.4 vs Gemini 3.1 Pro Benchmark April 2026](https://www.aimagicx.com/blog/claude-opus-4-6-vs-gpt-5-4-vs-gemini-3-1-benchmark-comparison-april-2026)
- [Claude 5 multimodal 2026](https://claude5.com/news/multimodal-ai-in-2026-vision-documents-real-world-applicatio)
- [Best AI Models January 2026 Fello](https://felloai.com/best-ai-of-january-2026/)
- [FTC BOTS Act refresher 2025](https://www.ftc.gov/business-guidance/blog/2025/04/bots-act-compliance-time-refresher)
- [TicketNews TM dismiss FTC lawsuit 2026](https://www.ticketnews.com/2026/01/ticketmaster-asks-court-to-dismiss-ftc-lawsuit-arguing-bots-act-doesnt-apply-to-it/)
- [Variety TM dismiss FTC](https://variety.com/2026/music/news/live-nation-ticketmaster-request-dismissal-ftc-lawsuit-1236625607/)
- [Rolling Stone FTC senator](https://www.rollingstone.com/music/music-news/senator-calls-on-ftc-to-enforce-bots-act-1235195779/)
- [BOTS Act Wikipedia](https://en.wikipedia.org/wiki/Better_Online_Tickets_Sales_Act)
- [ZenRows Shape F5 bypass 2026](https://www.zenrows.com/blog/bypass-f5)
- [Shape F5 Roundproxies](https://roundproxies.com/blog/f5-bypass/)
- [F5 Distributed Cloud Bot Defense](https://www.f5.com/products/distributed-cloud-services/bot-defense)
- [Ticketmaster Seated partnership](https://news.pollstar.com/2026/04/13/ticketmaster-seated-announce-partnership/)
- [Grafana OTel 2026](https://grafana.com/blog/opentelemetry-and-grafana-labs-whats-new-and-whats-next-in-2026/)
- [Playwright anti-bot AlterLab 2026](https://alterlab.io/blog/playwright-anti-bot-detection-what-actually-works-in-2026)
- [From Puppeteer to Nodriver Castle.io](https://blog.castle.io/from-puppeteer-stealth-to-nodriver-how-anti-detect-frameworks-evolved-to-evade-bot-detection/)
- [Playwright stealth 7 patches Dev.to 2026](https://dev.to/vhub_systems_ed5641f65d59/playwright-stealth-mode-in-2026-the-7-patches-that-actually-matter-46bp)
- [mitmproxy docs](https://docs.mitmproxy.org/stable/)
- [mitmproxy-mcp GitHub](https://github.com/snapspecter/mitmproxy-mcp)
- [Cloudflare Reverse Eng React bot detection 2026](https://dev.to/vhub_systems_ed5641f65d59/reverse-engineering-cloudflares-react-based-bot-detection-in-2026-5hl9)
- [Tor bridges](https://bridges.torproject.org/)
- [Imperva ticket scalping](https://www.imperva.com/learn/application-security/ticket-scalping-bots/)
- [DataDome stop scalper bots](https://datadome.co/learning-center/how-to-stop-scalper-bots-and-prevent-scalping/)
- [Kasada stop scalper bots](https://www.kasada.io/scalper-bots/)
- [Adyen Dynamic 3DS](https://docs.adyen.com/risk-management/dynamic-3d-secure)
- [Adyen tokenization](https://docs.adyen.com/online-payments/tokenization)
- [Stripe 3DS auth flow](https://docs.stripe.com/payments/3d-secure/authentication-flow)

---

**END OF REPORT** · generated 2026-04-22
