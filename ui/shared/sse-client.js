/* ============================================================
 * TM Diagnostics — SSE Client with auto-reconnect
 * ============================================================
 * Single source of truth para conexión SSE en UIs Diagnostics.
 *
 * Uso:
 *   const client = new TMSSEClient({
 *     url: "/api/diagnostics/events/stream",
 *     onEvent: (rec) => { ... },
 *     onConnect: () => { ... },
 *     onDisconnect: (reason) => { ... },
 *   });
 *   client.connect();
 *
 * Características:
 *   - reconnect exponencial con backoff
 *   - keep-alive ignore (líneas que empiezan por ":")
 *   - hello event detectado
 *   - graceful close on page unload
 * ============================================================ */

(function (global) {
  "use strict";

  function TMSSEClient(opts) {
    this.url = opts.url || "/api/diagnostics/events/stream";
    this.onEvent = opts.onEvent || function () {};
    this.onConnect = opts.onConnect || function () {};
    this.onDisconnect = opts.onDisconnect || function () {};
    this.maxBackoffMs = opts.maxBackoffMs || 10000;
    this.initialBackoffMs = opts.initialBackoffMs || 1000;
    this._es = null;
    this._backoffMs = this.initialBackoffMs;
    this._reconnectTimer = null;
    this._connected = false;
    this._closed = false;
  }

  TMSSEClient.prototype.connect = function () {
    var self = this;
    if (this._closed) return;
    try {
      this._es = new EventSource(this.url);
    } catch (e) {
      this._scheduleReconnect();
      return;
    }
    this._es.addEventListener("hello", function () {
      self._connected = true;
      self._backoffMs = self.initialBackoffMs;
      try { self.onConnect(); } catch (e) {}
    });
    this._es.onmessage = function (e) {
      try {
        var rec = JSON.parse(e.data);
        self.onEvent(rec);
      } catch (err) {}
    };
    this._es.onerror = function () {
      self._connected = false;
      try { self.onDisconnect("error"); } catch (e) {}
      try { self._es.close(); } catch (e) {}
      self._scheduleReconnect();
    };
  };

  TMSSEClient.prototype._scheduleReconnect = function () {
    var self = this;
    if (this._closed) return;
    if (this._reconnectTimer) return;
    var ms = this._backoffMs;
    this._reconnectTimer = setTimeout(function () {
      self._reconnectTimer = null;
      self.connect();
    }, ms);
    this._backoffMs = Math.min(this._backoffMs * 2, this.maxBackoffMs);
  };

  TMSSEClient.prototype.close = function () {
    this._closed = true;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this._es) {
      try { this._es.close(); } catch (e) {}
      this._es = null;
    }
  };

  TMSSEClient.prototype.isConnected = function () {
    return this._connected;
  };

  global.TMSSEClient = TMSSEClient;
})(typeof window !== "undefined" ? window : globalThis);
