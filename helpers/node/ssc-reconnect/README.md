# @delimitus/ssc-reconnect

Keep a WebSocket or an event stream going past the platform's limit. Zero dependencies; Node 22 or newer.

Cloud Run ends every request at the limit the gateway reports (5 minutes for most apps, 60 for session apps), and an open WebSocket or event stream is one request. The gateway tells the app when in the `X-SSC-Request-Deadline` header. These helpers end the stream cleanly a little before then, and the browser opens a new one by itself.

```js
import { browserClient, closeBeforeDeadline, endBeforeDeadline, secondsLeft } from '@delimitus/ssc-reconnect';

// Event stream: EventSource reconnects by itself, with Last-Event-ID if your events carry an id.
res.writeHead(200, { 'content-type': 'text/event-stream' });
endBeforeDeadline(res, req.headers);

// WebSocket (the `ws` package or anything with close(code, reason)): closed with code 1012.
wss.on('connection', (socket, req) => closeBeforeDeadline(socket, req.headers));

// The browser side of the WebSocket: serve this file and use sscSocket() instead of new WebSocket().
app.get('/ssc-reconnect.js', (_req, res) => res.type('text/javascript').send(browserClient()));

secondsLeft(req.headers); // seconds until the limit; null when run without the gateway
```

In the page:

```html
<script src="/ssc-reconnect.js"></script>
<script>
  let last = -1;
  const socket = sscSocket(() => `wss://${location.host}/ws?after=${last}`, {
    onopen() { /* runs again after every reconnect: send what the server needs */ },
    onmessage(event) { last = Number(event.data); },
  });
</script>
```

`sscSocket` reconnects at once after code 1012 and after a growing pause on an unclean drop. It gives up, calling `onclose`, after 10 reconnects in a row that did not stay open, or on a clean close with any other code. Options: `protocols`, `onopen`, `onmessage`, `onclose`, `delayMs` (250), `maxDelayMs` (10000), `attempts` (10).

Both server helpers take `{ margin }` in seconds (30) and `endBeforeDeadline` also `{ retryMs }` (1000). Both return a function that cancels, and cancel themselves when the stream closes first. A stream that opens with less than the margin left is ended after half the time left (`secondsToWait`), never at once. Without the header, as on a laptop, nothing is cut.

A reconnect is a new connection. Keep anything that must outlive one in Postgres or in the browser. Streamlit, Gradio, Dash and Shiny use their own connections and cannot use this; see the session-app note in `docs/contracts/manifest.md`.

`npm test` runs the shared vectors in `conformance/reconnect/vectors.json`, the same ones the Python helper `ssc_app.reconnect` runs, and holds a stream across a limit shortened to two seconds.
