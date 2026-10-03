import express from 'express';
import pg from 'pg';

// The connection string is pre-existing infrastructure. This app was written
// against a database that already had data in it, maintained by a different
// system, and shared with other consumers.
const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });

const app = express();

app.get('/api/headcount', async (_req, res) => {
  // Note what is absent: this app never creates `employees` or `departments`.
  // It has no migrations, no schema file and no seed. It is a reader.
  const { rows } = await pool.query(
    `select d.name as department, count(*)::int as headcount
       from employees e
       join departments d on d.id = e.department_id
      where e.active
      group by d.name
      order by headcount desc`,
  );
  res.json(rows);
});

app.get('/api/cost-centres', async (_req, res) => {
  const { rows } = await pool.query('select code, owner_email from cost_centres order by code');
  res.json(rows);
});

app.get('/', (_req, res) => res.send('<h1>Regional headcount</h1><div id="app"></div>'));

app.listen(process.env.PORT || 3000);
