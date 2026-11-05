/* ============================================================
 * TM Diagnostics — Reusable Panel Components
 * ============================================================
 * Funciones puras que toman datos + container y renderizan
 * componentes consistentes entre páginas.
 *
 * Cero dependencias. Funciona con vanilla JS.
 * Compatible con tokens.css (clases .tm-*).
 *
 * Uso:
 *   TMPanels.renderState(containerEl, {state, previous, history});
 *   TMPanels.renderHealth(containerEl, {score, verdict, ...});
 *   TMPanels.renderEvent(streamEl, record);
 *   TMPanels.renderWatchdogBanner(bannerEl, healthInfo);
 * ============================================================ */

(function (global) {
  "use strict";

  function html(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  }

  function verdictFor(score) {
    if (score >= 80) return "healthy";
    if (score >= 60) return "degraded";
    if (score >= 30) return "unhealthy";
    return "critical";
  }

  var TMPanels = {

    // ──────────────────────────────────────────────────────────────
    //  State panel (big badge + previous + history)
    // ──────────────────────────────────────────────────────────────
    renderState: function (el, data) {
      if (!el) return;
      var state = (data && data.state ? String(data.state) : "setup").toLowerCase();
      var previous = data && data.previous ? String(data.previous).toUpperCase() : "—";
      var bigClass = "tm-state-big " + state;
      var stateUpper = state.toUpperCase();
      var historyHTML = "";
      if (data && Array.isArray(data.history) && data.history.length > 0) {
        historyHTML = "<div class='tm-history'>";
        data.history.slice(-6).reverse().forEach(function (h) {
          var f = h.from || "?";
          var t = h.to || "?";
          var v = h.via || "";
          historyHTML += "<div class='tm-history-item'>" + html(f) +
                          " → <strong>" + html(t) + "</strong> via " + html(v) + "</div>";
        });
        historyHTML += "</div>";
      }
      el.innerHTML =
        "<div class='" + bigClass + "'>" + html(stateUpper) + "</div>" +
        "<div class='tm-state-previous'>previous: " + html(previous) + "</div>" +
        historyHTML;
    },

    // ──────────────────────────────────────────────────────────────
    //  Health gauge (score + verdict + bar)
    // ──────────────────────────────────────────────────────────────
    renderHealth: function (el, data) {
      if (!el) return;
      var score = (data && typeof data.score === "number") ? data.score : 0;
      var verdict = (data && data.verdict) ? String(data.verdict) : verdictFor(score);
      var verdictLower = verdict.toLowerCase();
      var widthPct = Math.max(0, Math.min(100, score));
      el.innerHTML =
        "<div class='tm-health-gauge'>" +
          "<div class='num " + verdictLower + "'>" + html(score) + "</div>" +
          "<div class='verdict'>" + html(verdict.toUpperCase()) + "</div>" +
        "</div>" +
        "<div class='tm-gauge-bar'><div class='fill' style='width:" + widthPct + "%'></div></div>";
    },

    // ──────────────────────────────────────────────────────────────
    //  Event stream line (push a streamEl)
    // ──────────────────────────────────────────────────────────────
    renderEvent: function (streamEl, rec, maxLines) {
      if (!streamEl || !rec) return;
      var max = maxLines || 200;
      var kind = rec.kind || "UNKNOWN";
      var ts = (rec.ts || "").substring(11, 19);
      var state = rec.state || "—";
      var evt = rec.event || "?";
      var isDrift = !!rec.drift;
      var isSoftDrift = isDrift && rec.drift && rec.drift.kind === "SOFT_DRIFT";
      var extraClass = isSoftDrift ? " soft-drift" : (isDrift ? " drift" : "");

      var line = document.createElement("div");
      line.className = "tm-event-line " + kind + extraClass;
      line.innerHTML =
        "<span class='ts' style='color:var(--tm-text-muted);min-width:70px'>" + ts + "</span>" +
        "<span class='tm-kind-badge " + kind + "'>" + kind + "</span>" +
        "<span class='state' style='color:var(--tm-text-muted);min-width:60px'>" + html(state) + "</span>" +
        "<span class='evt' style='flex:1'>" + html(evt) + "</span>";
      streamEl.appendChild(line);
      while (streamEl.children.length > max) {
        streamEl.removeChild(streamEl.firstChild);
      }
      streamEl.scrollTop = streamEl.scrollHeight;
    },

    // ──────────────────────────────────────────────────────────────
    //  Drift list panel
    // ──────────────────────────────────────────────────────────────
    renderDrifts: function (el, drifts) {
      if (!el) return;
      if (!Array.isArray(drifts) || drifts.length === 0) {
        el.innerHTML = "<div class='tm-empty'>No drifts</div>";
        return;
      }
      var html_ = "";
      drifts.slice(-8).reverse().forEach(function (d) {
        var k = d.kind || "?";
        var ev = d.event || "?";
        var detail = d.detail || "";
        var severity = d.severity || "info";
        var softInfo = "";
        if (d.soft_drift && d.soft_drift.remediation) {
          softInfo = "<div style='font-size:10px;color:var(--tm-text-muted);margin-top:3px'>" +
                      "→ " + html(d.soft_drift.remediation.substring(0, 100)) + "</div>";
        }
        html_ +=
          "<div class='tm-alert " + (severity === "high" ? "critical" : "warn") + "'>" +
            "<div style='font-weight:700'>" + html(k) + "</div>" +
            "<div style='font-size:11px'>event: <strong>" + html(ev) + "</strong></div>" +
            "<div style='font-size:10px;color:var(--tm-text-muted)'>" + html(detail) + "</div>" +
            softInfo +
          "</div>";
      });
      el.innerHTML = html_;
    },

    // ──────────────────────────────────────────────────────────────
    //  Watchdog banner (M6)
    // ──────────────────────────────────────────────────────────────
    renderWatchdogBanner: function (el, healthInfo) {
      if (!el) return;
      if (!healthInfo) {
        el.classList.remove("visible");
        return;
      }
      var mode = healthInfo.mode || "UNKNOWN";
      if (mode === "FULL") {
        el.classList.remove("visible");
        el.textContent = "";
        return;
      }
      var msg = "";
      if (mode === "TAILER_DEAD") {
        msg = "⚠ TAILER DEAD — Observability layer is not running. Diagnostics may be stale.";
      } else if (mode === "NO_AGGREGATE") {
        msg = "⚠ TAILER STALLED — last poll " + (healthInfo.last_poll_ago_s || "?") + "s ago.";
      } else if (mode === "TAILER_NOT_LOADED") {
        msg = "ℹ Diagnostics module not initialized.";
      } else {
        msg = "⚠ Mode: " + mode;
      }
      el.textContent = msg;
      el.classList.add("visible");
    },
  };

  global.TMPanels = TMPanels;
})(typeof window !== "undefined" ? window : globalThis);
