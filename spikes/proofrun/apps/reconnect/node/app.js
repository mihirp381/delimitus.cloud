// A WebSocket kept past the 60-minute limit with @delimitus/ssc-reconnect (SSC-090).
import http from "node:http";
import { WebSocketServer } from "ws";
import { closeBeforeDeadline, secondsLeft } from "@delimitus/ssc-reconnect";

const server = http.createServer((_req, res) => {
  res.writeHead(200, { "content-type": "application/json" });
  res.end('{"ok":true}');
});
const wss = new WebSocketServer({ server, path: "/ws" });
wss.on("connection", (socket, req) => {
  let n = Number(new URL(req.url, "http://x").searchParams.get("after") ?? -1);
  console.log(`OPEN after=${n} left=${secondsLeft(req.headers)}`);
  closeBeforeDeadline(socket, req.headers);
  const tick = setInterval(() => socket.send(String(++n)), 1000);
  socket.on("close", () => {
    clearInterval(tick);
    console.log(`CLOSED at=${n}`);
  });
});
server.listen(Number(process.env.PORT || 8080));
