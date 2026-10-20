-- SSC data gateway: the read-only service user a Snowflake connection logs in as (GA-5 B8).
--
-- Run it as ACCOUNTADMIN, in a worksheet or with SnowSQL, after setting five session
-- variables (a sixth, ssc_role, is optional). The public key is the one SSC gives you: paste it with or without its
-- -----BEGIN/END PUBLIC KEY----- lines. Names are plain identifiers (letters, digits and _),
-- matched without case as Snowflake matches unquoted names.
--
--   SET ssc_public_key = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA...';
--   SET ssc_user = 'SSC_DATAGW';
--   SET ssc_warehouse = 'REPORTING_WH';
--   SET ssc_database = 'ANALYTICS';
--   SET ssc_schemas = 'PUBLIC,FINANCE';
--   -- optional: SET ssc_role = 'SSC_DATAGW_REPORTING_READ';
--   -- then run this file:  snowsql -a <account> -u <admin> -f snowflake_setup.sql
--
-- Variables:
--   ssc_public_key  the RSA public key the user signs in with (required); no password is set
--   ssc_user        the user's name (required; SSC_DATAGW unless you have a reason)
--   ssc_warehouse   the warehouse the reads run on (required); it is billed for them
--   ssc_database    the database the connection reads (required)
--   ssc_schemas     the schemas whose tables and views the user may read, comma-separated
--   ssc_role        the role the user reads as (default <ssc_user>_READ, SSC_DATAGW_READ for SSC_DATAGW)
--
-- One user and one role per connection: a role holds the union of every schema granted to it,
-- so two connections sharing one would let an app granted one schema list read the other's.
-- For a second connection, set another ssc_user (its role follows) and run the script again.
--
-- The script makes the role and grants it USAGE on the warehouse, the database
-- and each schema, and SELECT on every table and view in each schema, now and future. It makes
-- the user with TYPE = SERVICE (key-pair sign-in only, no password, no MFA prompt), the public
-- key, the role as its only default role, and DEFAULT_SECONDARY_ROLES = () so no other role it
-- may hold is active in its sessions. It revokes nothing: a grant made by hand to the role or
-- to the user stays, and the readback at the end shows it. It is safe to run
-- again, which is how a new key or another schema lands.
--
-- The names are checked by one query, the grants walk a cursor of their own, and the script
-- fails unless it granted every schema it was given.

USE ROLE ACCOUNTADMIN;
-- ssc_read_role, not ssc_role, so a second run in this session with another ssc_user gets its own.
SET ssc_read_role = COALESCE(GETVARIABLE('SSC_ROLE'), UPPER(TRIM($ssc_user)) || '_READ');

EXECUTE IMMEDIATE $$
DECLARE
    usr VARCHAR DEFAULT UPPER(TRIM($ssc_user));
    wh VARCHAR DEFAULT UPPER(TRIM($ssc_warehouse));
    db VARCHAR DEFAULT UPPER(TRIM($ssc_database));
    rl VARCHAR DEFAULT UPPER(TRIM($ssc_read_role));
    schema_list VARCHAR DEFAULT UPPER($ssc_schemas);
    pk VARCHAR DEFAULT REGEXP_REPLACE($ssc_public_key, '-----[A-Z ]+-----|[[:space:]]', '');
    sch VARCHAR;
    not_a_name EXCEPTION (-20001, 'ssc_user, ssc_role, ssc_warehouse, ssc_database and each of ssc_schemas must be letters, digits and _ only');
    not_a_key EXCEPTION (-20002, 'ssc_public_key is not the base64 of an RSA public key');
    no_schema EXCEPTION (-20003, 'name at least one schema in ssc_schemas');
    not_granted EXCEPTION (-20004, 'a schema in ssc_schemas was not granted; run the script again and read the grants it shows');
BEGIN
    IF (NOT (usr RLIKE '[A-Z_][A-Z0-9_]{0,254}' AND wh RLIKE '[A-Z_][A-Z0-9_]{0,254}'
             AND db RLIKE '[A-Z_][A-Z0-9_]{0,254}' AND rl RLIKE '[A-Z_][A-Z0-9_]{0,254}')) THEN
        RAISE not_a_name;
    END IF;
    IF (NOT pk RLIKE '[A-Za-z0-9+/]{200,}={0,2}') THEN
        RAISE not_a_key;
    END IF;
    LET named INTEGER := 0;
    LET bad INTEGER := 0;
    SELECT COUNT(*), COUNT_IF(NOT TRIM(value) RLIKE '[A-Z_][A-Z0-9_]{0,254}')
      INTO :named, :bad
      FROM TABLE(SPLIT_TO_TABLE(:schema_list, ','))
     WHERE TRIM(value) <> '';
    IF (bad > 0) THEN
        RAISE not_a_name;
    END IF;
    IF (named = 0) THEN
        RAISE no_schema;
    END IF;

    EXECUTE IMMEDIATE 'CREATE ROLE IF NOT EXISTS ' || rl;
    EXECUTE IMMEDIATE 'CREATE USER IF NOT EXISTS ' || usr || ' TYPE = SERVICE';
    EXECUTE IMMEDIATE 'ALTER USER ' || usr || ' UNSET PASSWORD';
    EXECUTE IMMEDIATE 'ALTER USER ' || usr || ' SET TYPE = SERVICE'
        || ' RSA_PUBLIC_KEY = ''' || pk || ''''
        || ' DEFAULT_ROLE = ' || rl
        || ' DEFAULT_SECONDARY_ROLES = ()'
        || ' DEFAULT_WAREHOUSE = ' || wh
        || ' DEFAULT_NAMESPACE = ' || db;
    EXECUTE IMMEDIATE 'GRANT ROLE ' || rl || ' TO USER ' || usr;
    EXECUTE IMMEDIATE 'GRANT USAGE ON WAREHOUSE ' || wh || ' TO ROLE ' || rl;
    EXECUTE IMMEDIATE 'GRANT USAGE ON DATABASE ' || db || ' TO ROLE ' || rl;

    LET schemas RESULTSET := (
        SELECT TRIM(value) AS name FROM TABLE(SPLIT_TO_TABLE(:schema_list, ','))
        WHERE TRIM(value) <> ''
    );
    LET granted INTEGER := 0;
    LET granting CURSOR FOR schemas;
    FOR item IN granting DO
        sch := db || '.' || item.name;
        EXECUTE IMMEDIATE 'GRANT USAGE ON SCHEMA ' || sch || ' TO ROLE ' || rl;
        EXECUTE IMMEDIATE 'GRANT SELECT ON ALL TABLES IN SCHEMA ' || sch || ' TO ROLE ' || rl;
        EXECUTE IMMEDIATE 'GRANT SELECT ON ALL VIEWS IN SCHEMA ' || sch || ' TO ROLE ' || rl;
        EXECUTE IMMEDIATE 'GRANT SELECT ON FUTURE TABLES IN SCHEMA ' || sch || ' TO ROLE ' || rl;
        EXECUTE IMMEDIATE 'GRANT SELECT ON FUTURE VIEWS IN SCHEMA ' || sch || ' TO ROLE ' || rl;
        granted := granted + 1;
    END FOR;
    IF (granted <> named) THEN
        RAISE not_granted;
    END IF;
    RETURN rl || ' granted on ' || granted || ' schema(s) of ' || db || ' for ' || usr;
END;
$$;

-- What the role holds: USAGE on the warehouse, the database and each schema, SELECT on each
-- table and view. Anything else here was granted by hand; revoke it.
SHOW GRANTS TO ROLE IDENTIFIER($ssc_read_role);
SELECT "privilege", "granted_on", "name"
  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
 ORDER BY "granted_on", "name", "privilege";

-- The user: TYPE SERVICE, DEFAULT_ROLE the role above, and RSA_PUBLIC_KEY_FP, which must equal
-- the SHA256 fingerprint SSC gave you with the public key.
DESC USER IDENTIFIER($ssc_user);

-- The roles the user holds: the role above (and PUBLIC, which every user holds).
SHOW GRANTS TO USER IDENTIFIER($ssc_user);
