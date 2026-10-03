/**
 * A WebSocket that comes back by itself (SSC-090). No dependencies; serve or inline it in a page
 * as a plain script, then:
 *
 *   let last = -1;
 *   const socket = sscSocket(() => `wss://${location.host}/ws?after=${last}`, {
 *     onopen() { ... },                        // runs again after every reconnect
 *     onmessage(event) { last = ...; },
 *   });
 *   socket.send('hello');                      // false while it is reconnecting
 *
 * The app closes its end with code 1012 a little before the limit the gateway reports
 * (`close_before_deadline` in Python, `closeBeforeDeadline` in Node); this reconnects at once on
 * that close, and after a growing pause on an unclean drop. It gives up, calling `onclose`, after
 * `attempts` reconnects in a row that did not stay open, or on a clean close with any other code.
 * `url` may be a function, so a reconnect can say where to resume.
 */
(function (root) {
  const RESTART_CODE = 1012;

  /**
   * @param {string | (() => string)} url
   * @param {{protocols?: string | string[], onopen?: (event: Event) => void,
   *   onmessage?: (event: MessageEvent) => void, onclose?: (event: CloseEvent) => void,
   *   delayMs?: number, maxDelayMs?: number, attempts?: number}} [options]
   */
  function sscSocket(url, options = {}) {
    const { protocols, onopen, onmessage, onclose, delayMs = 250, maxDelayMs = 10000, attempts = 10 } = options;
    let ws = null;
    let timer = null;
    let failures = 0;
    let openedAt = 0;
    let stopped = false;

    function stop(event) {
      stopped = true;
      if (onclose) onclose(event);
    }

    function open() {
      timer = null;
      openedAt = 0;
      ws = new WebSocket(typeof url === 'function' ? url() : url, protocols);
      ws.onopen = (event) => {
        openedAt = Date.now();
        if (onopen) onopen(event);
      };
      ws.onmessage = (event) => {
        if (onmessage) onmessage(event);
      };
      ws.onclose = (event) => {
        if (stopped) return;
        const restart = event.code === RESTART_CODE;
        if (event.wasClean && !restart) return stop(event);
        if (openedAt !== 0 && Date.now() - openedAt >= maxDelayMs) failures = 0;
        const wait = restart && failures === 0 ? 0 : Math.min(maxDelayMs, delayMs * 2 ** failures);
        failures += 1;
        if (failures > attempts) return stop(event);
        timer = setTimeout(open, wait);
      };
    }

    open();
    return {
      get socket() {
        return ws;
      },
      send(data) {
        if (!ws || ws.readyState !== 1) return false;
        ws.send(data);
        return true;
      },
      close(code = 1000, reason = '') {
        stopped = true;
        clearTimeout(timer);
        if (ws) ws.close(code, reason);
      },
    };
  }

  root.sscSocket = sscSocket;
})(globalThis);
