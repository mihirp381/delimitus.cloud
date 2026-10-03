-- SSC data gateway: the read-only role a connection logs in as (SSC-051).
--
-- Run it with psql 10 or later, on the primary, as the database's owner or a superuser, in the
-- database the connection will read. Point the connection at a read replica when there is one;
-- the role and its grants reach the replica from the primary.
--
--   psql "host=db.example.com dbname=sales user=owner sslmode=verify-full" \
--        -v schemas=reporting,finance -f postgres_setup.sql
--
-- Variables (psql -v name=value):
--   schemas           schemas whose tables and views the role may read, comma-separated
--   relations         single tables or views it may read, as schema.name, comma-separated
--   role              the role's name (default ssc_datagw)
--   connection_limit  at most this many sessions at once (default 20)
--   password          set the password without a prompt; without it psql asks, and sends only
--                     a SCRAM verifier, so the password never reaches the server's logs
--
-- The role may log in, use the schemas named, and SELECT from their tables and views; nothing
-- else. It is no superuser, creates nothing, has no temporary tables, inherits no other role,
-- and its sessions start read-only. Postgres gives every role TEMPORARY on a database through
-- PUBLIC (and, in a database first made before Postgres 15, CREATE on schema public), which no
-- grant to one role can take away. Where PUBLIC holds either, this script grants it to each
-- role that exists now, then revokes it from PUBLIC: existing roles keep what they had, and
-- roles created later get it only when granted. The script is safe to run again, which is how
-- tables added to a schema later become readable.

\set ON_ERROR_STOP on
\if :{?role}
\else
\set role ssc_datagw
\endif
\if :{?schemas}
\else
\set schemas ''
\endif
\if :{?relations}
\else
\set relations ''
\endif
\if :{?connection_limit}
\else
\set connection_limit 20
\endif

SET ssc.role = :'role';
SET ssc.schemas = :'schemas';
SET ssc.relations = :'relations';
SET ssc.connection_limit = :'connection_limit';

BEGIN;

DO $setup$
DECLARE
    r text := current_setting('ssc.role');
    db text := current_database();
    lim int := current_setting('ssc.connection_limit')::int;
    attrs text := 'LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS';
    schemas text[] := coalesce(string_to_array(nullif(current_setting('ssc.schemas'), ''), ','), '{}');
    relations text[] := coalesce(string_to_array(nullif(current_setting('ssc.relations'), ''), ','), '{}');
    others text[];
    item text;
    other text;
    granted regclass;
BEGIN
    IF r !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
        RAISE EXCEPTION 'role must be a lower-case name of a-z, 0-9 and _';
    END IF;
    IF cardinality(schemas) = 0 AND cardinality(relations) = 0 THEN
        RAISE EXCEPTION 'name at least one schema (-v schemas=...) or relation (-v relations=...)';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = r) THEN
        EXECUTE format('ALTER ROLE %I %s CONNECTION LIMIT %s', r, attrs, lim);
    ELSE
        EXECUTE format('CREATE ROLE %I %s CONNECTION LIMIT %s', r, attrs, lim);
    END IF;
    EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', r);
    EXECUTE format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', r, '60s');
    FOR other IN
        SELECT g.rolname FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles g ON g.oid = m.roleid
        JOIN pg_catalog.pg_roles u ON u.oid = m.member
        WHERE u.rolname = r
    LOOP
        EXECUTE format('REVOKE %I FROM %I', other, r);
    END LOOP;

    SELECT coalesce(array_agg(rolname ORDER BY rolname), '{}') INTO others
    FROM pg_catalog.pg_roles WHERE rolname <> r AND rolname !~ '^pg_';

    IF has_database_privilege('public', db, 'TEMPORARY') THEN
        FOREACH other IN ARRAY others LOOP
            EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO %I', db, other);
        END LOOP;
        EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', db);
    END IF;
    FOR item IN
        SELECT nspname FROM pg_catalog.pg_namespace WHERE has_schema_privilege('public', oid, 'CREATE')
    LOOP
        FOREACH other IN ARRAY others LOOP
            EXECUTE format('GRANT CREATE ON SCHEMA %I TO %I', item, other);
        END LOOP;
        EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM PUBLIC', item);
    END LOOP;
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', db, r);
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db, r);
    FOR item IN
        SELECT nspname FROM pg_catalog.pg_namespace WHERE has_schema_privilege(r, oid, 'CREATE')
    LOOP
        EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I', item, r);
    END LOOP;

    FOREACH item IN ARRAY schemas LOOP
        item := btrim(item);
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', item, r);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', item, r);
    END LOOP;
    FOREACH item IN ARRAY relations LOOP
        granted := btrim(item)::regclass;
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I',
                       (SELECT n.nspname FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                        WHERE c.oid = granted), r);
        EXECUTE format('GRANT SELECT ON %s TO %I', granted, r);
    END LOOP;

    IF has_database_privilege(r, db, 'CREATE') OR has_database_privilege(r, db, 'TEMPORARY')
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE has_schema_privilege(r, oid, 'CREATE'))
    THEN
        RAISE EXCEPTION 'role % can still create objects or temporary tables', r;
    END IF;
    RAISE NOTICE 'role % may read % tables and views', r, (
        SELECT count(*) FROM pg_catalog.pg_class c
        WHERE c.relkind IN ('r', 'v', 'm', 'f', 'p') AND has_table_privilege(r, c.oid, 'SELECT')
          AND c.relnamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
    );
END
$setup$;

COMMIT;

\if :{?password}
ALTER ROLE :"role" PASSWORD :'password';
\else
\password :role
\endif
