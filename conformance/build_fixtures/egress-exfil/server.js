const http = require("http");

const port = process.env.PORT || 8080;
http
  .createServer((req, res) => res.end("<h1>The build reached only the registries</h1>"))
  .listen(port, "0.0.0.0");
