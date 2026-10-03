-- Created by the builder for THIS app. Nothing else reads these tables.
create table if not exists signups (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text not null,
  dietary_notes text,
  created_at timestamptz not null default now()
);

create table if not exists sessions (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  starts_at timestamptz not null
);

alter table signups enable row level security;
create policy "insert own signup" on signups for insert with check (true);
