import React from 'react';
import { createRoot } from 'react-dom/client';
import { listSessions, submitSignup } from './db.js';

function App() {
  const [sessions, setSessions] = React.useState([]);
  React.useEffect(() => { listSessions().then(({ data }) => setSessions(data ?? [])); }, []);
  return (
    <form onSubmit={(e) => { e.preventDefault(); submitSignup(Object.fromEntries(new FormData(e.target))); }}>
      <input name="name" placeholder="Name" required />
      <input name="email" type="email" placeholder="Email" required />
      <select name="session_id">{sessions.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}</select>
      <button>Sign up</button>
    </form>
  );
}
createRoot(document.getElementById('root')).render(<App />);
