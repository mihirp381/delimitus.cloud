-- SSC data gateway: the read-only service user a Snowflake connection logs in as (GA-5 B8).
--
-- Run it as ACCOUNTADMIN, in a worksheet or with SnowSQL, after setting five session
-- variables. The public key is the one SSC gives you: paste it with or without its
-- -----BEGIN/END PUBLIC KEY----- lines. Names are plain identifiers (letters, digits and _),
-- matched without case as Snowflake matches unquoted names.
--
--   SET ssc_public_key = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA...';
--   SET ssc_user = 'SSC_DATAGW';
--   SET ssc_warehouse = 'REPORTING_WH';
--   SET ssc_database = 'ANALYTICS';
--   SET ssc_schemas = 'PUBLIC,FINANCE';
--   -- then run this file:  snowsql -a <account> -u <admin> -f snowflake_setup.sql
--
-- Variables:
--   ssc_public_key  the RSA public key the user signs in with (required); no password is set
--   ssc_user        the user's name (required; SSC_DATAGW unless you have a reason)
--   ssc_warehouse   the warehouse the reads run on (required); it is billed for them
--   ssc_database    the database the connection reads (required)
--   ssc_schemas     the schemas whose tables and views the user may read, comma-separated
--
-- The script makes the role SSC_DATAGW_READ and grants it USAGE on the warehouse, the database
-- and each schema, and SELECT on every table and view in each schema, now and future. It makes
-- the user with TYPE = SERVICE (key-pair sign-in only, no password, no MFA prompt), the public
-- key, SSC_DATAGW_READ as its only default role, and DEFAULT_SECONDARY_ROLES = () so no other
-- role it may hold is active in its sessions. It revokes nothing: a grant made by hand to
-- SSC_DATAGW_READ or to the user stays, and the readback at the end shows it. It is safe to run
-- again, which is how a new key or another schema lands.
--
-- Run on a live trial account on 2026-10-08 (GA-5.6). Its first form read the schema list
-- through one RESULTSET with two cursors, one to check the names and one to grant; the second
-- cursor granted nothing and the script still ended with "granted on 1 schema(s)". The role held
-- USAGE on the database and no schema, so every read was 002003 (object does not exist). Now
-- the names are checked by one query, the grants walk a cursor of their own, and the script
-- fails unless it granted every schema it was given.

USE ROLE ACCOUNTADMIN;

EXECUTE IMMEDIATE $$
DECLARE
    usr VARCHAR DEFAULT UPPER(TRIM($ssc_user));
    wh VARCHAR DEFAULT UPPER(TRIM($ssc_warehouse));
    db VARCHAR DEFAULT UPPER(TRIM($ssc_database));
    schema_list VARCHAR DEFAULT UPPER($ssc_schemas);
    pk VARCHAR DEFAULT REGEXP_REPLACE($ssc_public_key, '-----[A-Z ]+-----|[[:space:]]', '');
    sch VARCHAR;
    not_a_name EXCEPTION (-20001, 'ssc_user, ssc_warehouse, ssc_database and each of ssc_schemas must be letters, digits and _ only');
    not_a_key EXCEPTION (-20002, 'ssc_public_key is not the base64 of an RSA public key');
    no_schema EXCEPTION (-20003, 'name at least one schema in ssc_schemas');
    not_granted EXCEPTION (-20004, 'a schema in ssc_schemas was not granted; run the script again and read the grants it shows');
BEGIN
    IF (NOT (usr RLIKE '[A-Z_][A-Z0-9_]{0,254}' AND wh RLIKE '[A-Z_][A-Z0-9_]{0,254}'
             AND db RLIKE '[A-Z_][A-Z0-9_]{0,254}')) THEN
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

    EXECUTE IMMEDIATE 'CREATE ROLE IF NOT EXISTS SSC_DATAGW_READ';
    EXECUTE IMMEDIATE 'CREATE USER IF NOT EXISTS ' || usr || ' TYPE = SERVICE';
    EXECUTE IMMEDIATE 'ALTER USER ' || usr || ' UNSET PASSWORD';
    EXECUTE IMMEDIATE 'ALTER USER ' || usr || ' SET TYPE = SERVICE'
        || ' RSA_PUBLIC_KEY = ''' || pk || ''''
        || ' DEFAULT_ROLE = SSC_DATAGW_READ'
        || ' DEFAULT_SECONDARY_ROLES = ()'
        || ' DEFAULT_WAREHOUSE = ' || wh
        || ' DEFAULT_NAMESPACE = ' || db;
    EXECUTE IMMEDIATE 'GRANT ROLE SSC_DATAGW_READ TO USER ' || usr;
    EXECUTE IMMEDIATE 'GRANT USAGE ON WAREHOUSE ' || wh || ' TO ROLE SSC_DATAGW_READ';
    EXECUTE IMMEDIATE 'GRANT USAGE ON DATABASE ' || db || ' TO ROLE SSC_DATAGW_READ';

    LET schemas RESULTSET := (
        SELECT TRIM(value) AS name FROM TABLE(SPLIT_TO_TABLE(:schema_list, ','))
        WHERE TRIM(value) <> ''
    );
    LET granted INTEGER := 0;
    LET granting CURSOR FOR schemas;
    FOR item IN granting DO
        sch := db || '.' || item.name;
        EXECUTE IMMEDIATE 'GRANT USAGE ON SCHEMA ' || sch || ' TO ROLE SSC_DATAGW_READ';
        EXECUTE IMMEDIATE 'GRANT SELECT ON ALL TABLES IN SCHEMA ' || sch || ' TO ROLE SSC_DATAGW_READ';
        EXECUTE IMMEDIATE 'GRANT SELECT ON ALL VIEWS IN SCHEMA ' || sch || ' TO ROLE SSC_DATAGW_READ';
        EXECUTE IMMEDIATE 'GRANT SELECT ON FUTURE TABLES IN SCHEMA ' || sch || ' TO ROLE SSC_DATAGW_READ';
        EXECUTE IMMEDIATE 'GRANT SELECT ON FUTURE VIEWS IN SCHEMA ' || sch || ' TO ROLE SSC_DATAGW_READ';
        granted := granted + 1;
    END FOR;
    IF (granted <> named) THEN
        RAISE not_granted;
    END IF;
    RETURN 'SSC_DATAGW_READ granted on ' || granted || ' schema(s) of ' || db || ' for ' || usr;
END;
$$;

-- What the role holds: USAGE on the warehouse, the database and each schema, SELECT on each
-- table and view. Anything else here was granted by hand; revoke it.
SHOW GRANTS TO ROLE SSC_DATAGW_READ;
SELECT "privilege", "granted_on", "name"
  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
 ORDER BY "granted_on", "name", "privilege";

-- The user: TYPE SERVICE, DEFAULT_ROLE SSC_DATAGW_READ, and RSA_PUBLIC_KEY_FP, which must equal
-- the SHA256 fingerprint SSC gave you with the public key.
DESC USER IDENTIFIER($ssc_user);

-- The roles the user holds: SSC_DATAGW_READ (and PUBLIC, which every user holds).
SHOW GRANTS TO USER IDENTIFIER($ssc_user);
