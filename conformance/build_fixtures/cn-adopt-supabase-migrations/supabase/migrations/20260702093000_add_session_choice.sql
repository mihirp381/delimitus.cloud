alter table signups add column session_id uuid references sessions(id);
