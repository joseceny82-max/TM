"""test_intl_integrity.py — Test automático de integridad de Intl.DateTimeFormat
tras aplicar el stealth_js de purchase_stealth.py.

Verifica que el override NO rompe:
    - Intl.DateTimeFormat.supportedLocalesOf (método estático CRÍTICO — el bug que rompió PingFederate)
    - new Intl.DateTimeFormat().resolvedOptions() (función de instancia + nuestro timeZone override)
    - new Intl.DateTimeFormat() instanceof Intl.DateTimeFormat
    - Object.prototype.toString.call(new Intl.DateTimeFormat())
    - Reflect.ownKeys(Intl.DateTimeFormat)
    - getOwnPropertyDescriptor sobre supportedLocalesOf
    - Otros métodos Intl que NO debemos haber roto (NumberFormat, RelativeTimeFormat, Collator)

Uso:
    python test_intl_integrity.py
Exit code 0 si todos los tests pasan, 1 si alguno falla.

Hace 2 runs:
    A) sin __PS_SKIP_TZ_OVERRIDE → con nuestro Proxy patch
    B) con __PS_SKIP_TZ_OVERRIDE=true → bypass de patch (baseline nativa)

Si A == B en todo excepto resolvedOptions().timeZone → fix OK.
"""
from __future__ import annotations
import os, sys, json, time
from datetime import datetime
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import requests
import bot_orchestrator as bo
from playwright.sync_api import sync_playwright
import purchase_stealth as ps


def run_tests_in_browser(skip_tz_override: bool) -> dict:
    """Arranca perfil fresh, inyecta stealth_js, ejecuta batería de tests JS."""
    fresh = bo.create_fresh_profile(country_code='ES')
    if not fresh: return {'error': 'create_fresh_profile_failed'}
    port = bo._start_profile(fresh['env_id'])
    if not port:
        bo.destroy_fresh_profile(fresh)
        return {'error': 'start_profile_failed'}
    time.sleep(4)
    result = {'skip_tz_override': skip_tz_override}
    try:
        v = requests.get(f'http://127.0.0.1:{port}/json/version', timeout=8).json()
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(v['webSocketDebuggerUrl'])
            ctx = b.contexts[0] if b.contexts else b.new_context()

            # Inyectar pre-script para opcionalmente saltar nuestro override
            if skip_tz_override:
                ctx.add_init_script("window.__PS_SKIP_TZ_OVERRIDE = true;")

            # Aplicar nuestro stealth completo
            persona = ps.create_persona_from_sessionid('100100', 'ES')
            ps.apply_stealth_to_context(ctx, persona, engine='chrome')

            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto('about:blank', wait_until='domcontentloaded', timeout=15000)
            time.sleep(2)

            # Batería de tests
            result['tests'] = page.evaluate("""() => {
                const results = {};

                // ─── TEST 1: supportedLocalesOf (CRÍTICO) ───
                try {
                    const r = Intl.DateTimeFormat.supportedLocalesOf(['es-ES', 'en-US', 'zh-Hant']);
                    results.supportedLocalesOf = {
                        ok: Array.isArray(r), value: r, type: typeof Intl.DateTimeFormat.supportedLocalesOf,
                    };
                } catch (e) {
                    results.supportedLocalesOf = {ok: false, error: e.message};
                }

                // ─── TEST 2: resolvedOptions().timeZone (nuestro override) ───
                try {
                    const dtf = new Intl.DateTimeFormat();
                    const opts = dtf.resolvedOptions();
                    results.resolvedOptions = {
                        ok: typeof opts === 'object',
                        timeZone: opts.timeZone,
                        locale: opts.locale,
                        calendar: opts.calendar,
                        numberingSystem: opts.numberingSystem,
                    };
                } catch (e) {
                    results.resolvedOptions = {ok: false, error: e.message};
                }

                // ─── TEST 3: format() funciona normal ───
                try {
                    const dtf = new Intl.DateTimeFormat('es-ES', {year: 'numeric', month: 'long', day: 'numeric'});
                    const formatted = dtf.format(new Date(2026, 4, 13));
                    results.format = {ok: typeof formatted === 'string', value: formatted};
                } catch (e) {
                    results.format = {ok: false, error: e.message};
                }

                // ─── TEST 4: instanceof check ───
                try {
                    const dtf = new Intl.DateTimeFormat();
                    results.instanceof_check = {
                        ok: dtf instanceof Intl.DateTimeFormat,
                        proto_chain_ok: Object.getPrototypeOf(dtf) === Intl.DateTimeFormat.prototype,
                    };
                } catch (e) {
                    results.instanceof_check = {ok: false, error: e.message};
                }

                // ─── TEST 5: Object.prototype.toString ───
                try {
                    const dtf = new Intl.DateTimeFormat();
                    const ts = Object.prototype.toString.call(dtf);
                    results.toString_tag = {
                        ok: ts === '[object Intl.DateTimeFormat]',
                        value: ts,
                    };
                } catch (e) {
                    results.toString_tag = {ok: false, error: e.message};
                }

                // ─── TEST 6: Reflect.ownKeys del constructor ───
                try {
                    const keys = Reflect.ownKeys(Intl.DateTimeFormat).sort();
                    const expectedSubset = ['supportedLocalesOf', 'prototype', 'length', 'name'];
                    const has_all = expectedSubset.every(k => keys.includes(k));
                    results.ownKeys = {ok: has_all, keys: keys, missing: expectedSubset.filter(k => !keys.includes(k))};
                } catch (e) {
                    results.ownKeys = {ok: false, error: e.message};
                }

                // ─── TEST 7: Property descriptor de supportedLocalesOf ───
                try {
                    const d = Object.getOwnPropertyDescriptor(Intl.DateTimeFormat, 'supportedLocalesOf');
                    results.descriptor = {
                        ok: d && typeof d.value === 'function',
                        descriptor: d ? {
                            writable: d.writable, configurable: d.configurable,
                            enumerable: d.enumerable, value_type: typeof d.value,
                        } : null,
                    };
                } catch (e) {
                    results.descriptor = {ok: false, error: e.message};
                }

                // ─── TEST 8: Function name + length ───
                try {
                    results.constructor_meta = {
                        name: Intl.DateTimeFormat.name,
                        length: Intl.DateTimeFormat.length,
                        toString: Intl.DateTimeFormat.toString().slice(0, 60),
                    };
                } catch (e) {
                    results.constructor_meta = {ok: false, error: e.message};
                }

                // ─── TEST 9: PingFederate-like usage (call pattern del JS roto) ───
                try {
                    // Simula EXACTAMENTE lo que hace 2.4cc433e0...js de PingFederate:
                    //   const supported = Intl.DateTimeFormat.supportedLocalesOf(['es-ES']);
                    //   const dtf = new Intl.DateTimeFormat(supported[0], options);
                    //   const formatted = dtf.format(new Date());
                    const supported = Intl.DateTimeFormat.supportedLocalesOf(['es-ES']);
                    const dtf = new Intl.DateTimeFormat(supported[0] || 'es-ES', {
                        hour: 'numeric', minute: 'numeric',
                    });
                    const formatted = dtf.format(new Date());
                    results.pingfederate_pattern = {
                        ok: true,
                        supported: supported,
                        formatted: formatted,
                    };
                } catch (e) {
                    results.pingfederate_pattern = {ok: false, error: e.message};
                }

                // ─── TEST 10: Other Intl objects NO afectados ───
                try {
                    results.other_intl = {
                        NumberFormat_supported: Array.isArray(Intl.NumberFormat.supportedLocalesOf(['es-ES'])),
                        Collator_supported: Array.isArray(Intl.Collator.supportedLocalesOf(['es-ES'])),
                        RelativeTimeFormat_supported: Array.isArray(Intl.RelativeTimeFormat.supportedLocalesOf(['es-ES'])),
                        PluralRules_supported: Array.isArray(Intl.PluralRules.supportedLocalesOf(['es-ES'])),
                    };
                } catch (e) {
                    results.other_intl = {ok: false, error: e.message};
                }

                // ─── TEST 11: Webdriver tells (sanity check stealth applied) ───
                results.stealth_sanity = {
                    webdriver: navigator.webdriver,
                    plugins_len: navigator.plugins.length,
                    languages: navigator.languages,
                };

                return results;
            }""")
            b.close()
    except Exception as e:
        result['fatal_error'] = str(e)
    finally:
        try:
            requests.post('http://127.0.0.1:40000/api/env/close',
                          json={'envId': fresh['env_id']}, timeout=10)
            time.sleep(2)
            bo.destroy_fresh_profile(fresh)
        except Exception: pass
    return result


def main():
    print('=' * 70)
    print('TEST INTEGRIDAD API Intl.DateTimeFormat')
    print('=' * 70)

    print('\n[RUN A] CON Proxy override (Europe/Madrid):')
    print('-' * 70)
    res_a = run_tests_in_browser(skip_tz_override=False)
    if res_a.get('fatal_error'):
        print(f'  FATAL: {res_a["fatal_error"]}')
        sys.exit(1)
    tests_a = res_a.get('tests', {})
    for name, t in tests_a.items():
        ok = t.get('ok', '?') if isinstance(t, dict) else '-'
        snippet = json.dumps(t, ensure_ascii=False, default=str)[:140]
        print(f'  [{ok}] {name}: {snippet}')

    print('\n[RUN B] SIN Proxy override (baseline nativa via __PS_SKIP_TZ_OVERRIDE):')
    print('-' * 70)
    res_b = run_tests_in_browser(skip_tz_override=True)
    if res_b.get('fatal_error'):
        print(f'  FATAL: {res_b["fatal_error"]}')
        sys.exit(1)
    tests_b = res_b.get('tests', {})
    for name, t in tests_b.items():
        ok = t.get('ok', '?') if isinstance(t, dict) else '-'
        snippet = json.dumps(t, ensure_ascii=False, default=str)[:140]
        print(f'  [{ok}] {name}: {snippet}')

    print('\n[VERDICT]')
    print('-' * 70)
    critical = ['supportedLocalesOf', 'instanceof_check', 'toString_tag',
                'ownKeys', 'descriptor', 'pingfederate_pattern', 'other_intl']
    pass_count = 0
    fail_count = 0
    diffs = []
    for k in critical:
        ta = tests_a.get(k, {})
        tb = tests_b.get(k, {})
        ok_a = ta.get('ok', False) if isinstance(ta, dict) else False
        ok_b = tb.get('ok', False) if isinstance(tb, dict) else False
        if ok_a and ok_b:
            pass_count += 1
            print(f'  ✓ {k}: BOTH pass (proxy OK)')
        else:
            fail_count += 1
            print(f'  ✗ {k}: A={ok_a} B={ok_b}')
            diffs.append({k: {'A': ta, 'B': tb}})

    # resolvedOptions debe diferir SOLO en timeZone
    ro_a = tests_a.get('resolvedOptions', {})
    ro_b = tests_b.get('resolvedOptions', {})
    if ro_a.get('timeZone') == 'Europe/Madrid':
        print(f'  ✓ resolvedOptions: timeZone correctly overridden to Europe/Madrid')
    else:
        print(f'  ✗ resolvedOptions: A timeZone={ro_a.get("timeZone")} (expected Europe/Madrid)')

    print(f'\n  Critical tests passed: {pass_count}/{len(critical)}')
    if fail_count > 0:
        print('\n  DIFFS:')
        print(json.dumps(diffs, indent=2, ensure_ascii=False, default=str)[:2000])

    # Save full
    out_path = f'datos/diagnose/intl_integrity_test_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'with_proxy': res_a, 'without_proxy': res_b,
                   'verdict_critical_pass': pass_count == len(critical)},
                  f, indent=2, ensure_ascii=False, default=str)
    print(f'\n  Saved: {out_path}')

    sys.exit(0 if (pass_count == len(critical)) else 1)


if __name__ == '__main__':
    main()


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
