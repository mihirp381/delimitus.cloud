-- GA-5 · Connection kinds: the ten read-only sources a connection can point at.

-- kind widens from postgres alone to the ten kinds of ssc_contracts.connections. The address of
-- a SQL kind stays in host, port and database_name; every kind also keeps its full non-secret
-- address as JSON (the kind's address model, by alias, defaults included), which is what the
-- other kinds have instead of a host. The three columns are nullable from here: a SQL kind
-- always fills them (the CHECK), the other kinds never do. Rows from before are postgres rows
-- with an address built from their columns. Credentials are never here (SSC-051).
ALTER TABLE ssc.connection NO FORCE ROW LEVEL SECURITY;

ALTER TABLE ssc.connection
  DROP CONSTRAINT connection_kind_check,
  ADD CONSTRAINT connection_kind_check CHECK (kind IN (
    'postgres', 'mysql', 'sqlserver', 'bigquery', 'snowflake', 'gsheets', 'gcs', 's3',
    'airtable', 'rest')),
  ALTER COLUMN host DROP NOT NULL,
  ALTER COLUMN port DROP NOT NULL,
  ALTER COLUMN database_name DROP NOT NULL,
  ADD COLUMN address jsonb NOT NULL DEFAULT '{}'::jsonb
    CONSTRAINT connection_address_check CHECK (jsonb_typeof(address) = 'object');

UPDATE ssc.connection
  SET address = jsonb_build_object('host', host, 'port', port, 'database', database_name)
  WHERE address = '{}'::jsonb AND host IS NOT NULL;

ALTER TABLE ssc.connection
  ADD CONSTRAINT connection_sql_address_check CHECK (
    (kind IN ('postgres', 'mysql', 'sqlserver'))
      = (host IS NOT NULL AND port IS NOT NULL AND database_name IS NOT NULL)),
  ADD CONSTRAINT connection_address_set_check CHECK (address <> '{}'::jsonb);

ALTER TABLE ssc.connection FORCE ROW LEVEL SECURITY;
