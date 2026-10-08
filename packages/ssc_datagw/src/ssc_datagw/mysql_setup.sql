-- SSC data gateway: the read-only user a MySQL connection logs in as (GA-5).
--
-- Run it with the mysql client on MySQL 8.0 or later, on the primary, as an admin who may
-- create users, grant SELECT on the schemas named, read mysql.role_edges, and create a
-- procedure in the database the session starts in. Name that database on the command line
-- (one of the schemas to grant will do): the script needs a default database for a temporary
-- procedure, and stops with "No database selected" without one. Point the connection at a
-- read replica when there is one; the user and its grants reach the replica from the primary.
--
--   mysql -h db.example.com -u admin -p --ssl-mode=VERIFY_IDENTITY reporting
--   mysql> SET @schemas = 'reporting,finance';
--   mysql> SET @password = '<the password the connection will use>';
--   mysql> source mysql_setup.sql
--
-- Or without a prompt, which puts the password in the process list and the shell's history:
--
--   mysql -h db.example.com -u admin -p --ssl-mode=VERIFY_IDENTITY \
--         --init-command="SET @schemas='reporting,finance', @password='...'" \
--         reporting < mysql_setup.sql
--
-- Variables (SET @name = value before the script runs):
--   @schemas   schemas whose tables and views the user may read, comma-separated (required)
--   @password  the user's password (required)
--   @user      the user's name (default ssc_datagw); it may log in from any host, over TLS only
--
-- The user must log in over TLS, may hold at most 20 sessions at once, and may SELECT from the
-- named schemas' tables and views; nothing else. Every other privilege it held is revoked, every
-- role granted to it is revoked, and no role is active when it logs in. The script is safe to
-- run again, which is how a new password or another schema lands.
--
-- MySQL runs IF, WHILE and SIGNAL only inside a stored program, so the work is done by a
-- procedure, ssc_setup, made in the session's database and dropped again at the end: it exists
-- only while the script runs (a run that stops halfway leaves it, and the next run replaces it).
-- It is SQL SECURITY INVOKER, so while it exists anyone else who may execute procedures in that
-- database runs it with their own privileges, never with the admin's.

SET @user = IFNULL(NULLIF(@user, ''), 'ssc_datagw');
SET @schemas = TRIM(@schemas);

DROP PROCEDURE IF EXISTS ssc_setup;

DELIMITER //
CREATE PROCEDURE ssc_setup()
    SQL SECURITY INVOKER
BEGIN
    DECLARE account TEXT;
    DECLARE rest TEXT;
    DECLARE item TEXT;
    DECLARE granted_roles TEXT;

    IF @password IS NULL OR @password = '' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'set @password first';
    END IF;
    IF @schemas IS NULL OR @schemas = '' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'name at least one schema in @schemas';
    END IF;
    IF @@session.sql_mode LIKE '%NO_BACKSLASH_ESCAPES%' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'turn NO_BACKSLASH_ESCAPES off: QUOTE() escapes with backslashes';
    END IF;

    SET account = CONCAT(QUOTE(@user), '@', QUOTE('%'));

    SET @ssc_sql = CONCAT('CREATE USER IF NOT EXISTS ', account, ' IDENTIFIED BY ', QUOTE(@password),
                          ' REQUIRE SSL WITH MAX_USER_CONNECTIONS 20');
    PREPARE ssc_stmt FROM @ssc_sql;
    EXECUTE ssc_stmt;
    DEALLOCATE PREPARE ssc_stmt;

    SET @ssc_sql = CONCAT('ALTER USER ', account, ' IDENTIFIED BY ', QUOTE(@password),
                          ' REQUIRE SSL WITH MAX_USER_CONNECTIONS 20');
    PREPARE ssc_stmt FROM @ssc_sql;
    EXECUTE ssc_stmt;
    DEALLOCATE PREPARE ssc_stmt;

    SET @ssc_sql = CONCAT('REVOKE ALL PRIVILEGES, GRANT OPTION FROM ', account);
    PREPARE ssc_stmt FROM @ssc_sql;
    EXECUTE ssc_stmt;
    DEALLOCATE PREPARE ssc_stmt;

    SELECT GROUP_CONCAT(CONCAT(QUOTE(FROM_USER), '@', QUOTE(FROM_HOST)))
      INTO granted_roles
      FROM mysql.role_edges
     WHERE TO_USER = @user AND TO_HOST = '%';
    IF granted_roles IS NOT NULL THEN
        SET @ssc_sql = CONCAT('REVOKE ', granted_roles, ' FROM ', account);
        PREPARE ssc_stmt FROM @ssc_sql;
        EXECUTE ssc_stmt;
        DEALLOCATE PREPARE ssc_stmt;
    END IF;

    SET rest = @schemas;
    WHILE rest <> '' DO
        SET item = TRIM(SUBSTRING_INDEX(rest, ',', 1));
        SET rest = IF(LOCATE(',', rest) > 0, SUBSTRING(rest, LOCATE(',', rest) + 1), '');
        IF item <> '' THEN
            SET @ssc_sql = CONCAT('GRANT SELECT ON `', REPLACE(item, '`', '``'), '`.* TO ', account);
            PREPARE ssc_stmt FROM @ssc_sql;
            EXECUTE ssc_stmt;
            DEALLOCATE PREPARE ssc_stmt;
        END IF;
    END WHILE;

    SET @ssc_sql = CONCAT('SET DEFAULT ROLE NONE TO ', account);
    PREPARE ssc_stmt FROM @ssc_sql;
    EXECUTE ssc_stmt;
    DEALLOCATE PREPARE ssc_stmt;
    SET @ssc_sql = NULL;
END//
DELIMITER ;

CALL ssc_setup();
DROP PROCEDURE ssc_setup;

SELECT p.table_schema AS `schema`,
       p.privilege_type AS `privilege`,
       (SELECT COUNT(*) FROM information_schema.tables t
         WHERE t.table_schema = p.table_schema) AS tables_and_views
  FROM information_schema.schema_privileges p
 WHERE p.grantee = CONCAT(QUOTE(@user), '@', QUOTE('%'))
 ORDER BY p.table_schema, p.privilege_type;
