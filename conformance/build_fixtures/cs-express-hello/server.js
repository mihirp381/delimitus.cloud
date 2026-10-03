const express = require('express');
const app = express();
app.get('/', (_req, res) => res.send('<h1>Headcount lookup</h1>'));
app.get('/api/headcount', (_req, res) => res.json({ total: 412 }));
app.listen(process.env.PORT || 8080);
