-- SSC data gateway: the read-only user a MySQL connection logs in as (GA-5).
--
-- Run it with the mysql client on MySQL 8.0 or later, on the primary, as an admin who may
-- create users, grant SELECT on the schemas named, read the mysql schema's grant tables, and
-- create a procedure in the database the session starts in. Name that database on the command line
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
-- The privileges are revoked one grant at a time, each on the level it was granted, not with
-- REVOKE ALL PRIVILEGES, GRANT OPTION FROM the user: under partial_revokes an admin who may not
-- touch some schema cannot run that blanket form at all. Cloud SQL for MySQL is such a server:
-- its root has partial revokes on mysql and sys, and the blanket revoke stops with ERROR 3879,
-- "Access denied for AuthId root@% to database 'sys'" (found on Cloud SQL 8.4, 2026-10-08).
-- Before SELECT is granted the script reads the grants back, and stops if any is left.
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
    DECLARE revokes JSON;
    DECLARE i INT DEFAULT 0;
    DECLARE left_over INT;
    DECLARE concat_len INT DEFAULT @@session.group_concat_max_len;

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

    -- Every grant the user holds, as the REVOKE that takes it back on its own level.
    SET SESSION group_concat_max_len = 1048576;
    SELECT JSON_ARRAYAGG(statement) INTO revokes FROM (
        SELECT CONCAT('REVOKE ', privilege_type, ' ON *.* FROM ', account) AS statement
          FROM information_schema.user_privileges
         WHERE grantee = account AND privilege_type <> 'USAGE'
        UNION ALL
        SELECT DISTINCT CONCAT('REVOKE GRANT OPTION ON *.* FROM ', account)
          FROM information_schema.user_privileges
         WHERE grantee = account AND is_grantable = 'YES'
        UNION ALL
        SELECT CONCAT('REVOKE ', GROUP_CONCAT(privilege_type),
                      IF(MAX(is_grantable) = 'YES', ', GRANT OPTION', ''),
                      ' ON `', REPLACE(table_schema, '`', '``'), '`.* FROM ', account)
          FROM information_schema.schema_privileges
         WHERE grantee = account
         GROUP BY table_schema
        UNION ALL
        SELECT CONCAT('REVOKE ', GROUP_CONCAT(privilege_type),
                      IF(MAX(is_grantable) = 'YES', ', GRANT OPTION', ''),
                      ' ON `', REPLACE(table_schema, '`', '``'), '`.`',
                      REPLACE(table_name, '`', '``'), '` FROM ', account)
          FROM information_schema.table_privileges
         WHERE grantee = account
         GROUP BY table_schema, table_name
        UNION ALL
        SELECT CONCAT('REVOKE ', GROUP_CONCAT(on_columns SEPARATOR ', '),
                      ' ON `', REPLACE(table_schema, '`', '``'), '`.`',
                      REPLACE(table_name, '`', '``'), '` FROM ', account)
          FROM (SELECT table_schema, table_name,
                       CONCAT(privilege_type, ' (',
                              GROUP_CONCAT(CONCAT('`', REPLACE(column_name, '`', '``'), '`')),
                              ')') AS on_columns
                  FROM information_schema.column_privileges
                 WHERE grantee = account
                 GROUP BY table_schema, table_name, privilege_type) AS columns_held
         GROUP BY table_schema, table_name
        UNION ALL
        SELECT CONCAT('REVOKE ', REPLACE(Proc_priv, 'Grant', 'GRANT OPTION'), ' ON ', Routine_type,
                      ' `', REPLACE(Db, '`', '``'), '`.`', REPLACE(Routine_name, '`', '``'),
                      '` FROM ', account)
          FROM mysql.procs_priv
         WHERE User = @user AND Host = '%' AND Proc_priv <> ''
        UNION ALL
        SELECT CONCAT('REVOKE PROXY ON ', QUOTE(Proxied_user), '@', QUOTE(Proxied_host),
                      ' FROM ', account)
          FROM mysql.proxies_priv
         WHERE User = @user AND Host = '%'
    ) AS held;
    SET SESSION group_concat_max_len = concat_len;
    WHILE i < IFNULL(JSON_LENGTH(revokes), 0) DO
        SET @ssc_sql = JSON_UNQUOTE(JSON_EXTRACT(revokes, CONCAT('$[', i, ']')));
        PREPARE ssc_stmt FROM @ssc_sql;
        EXECUTE ssc_stmt;
        DEALLOCATE PREPARE ssc_stmt;
        SET i = i + 1;
    END WHILE;

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

    SELECT (SELECT COUNT(*) FROM information_schema.user_privileges
             WHERE grantee = account AND (privilege_type <> 'USAGE' OR is_grantable = 'YES'))
         + (SELECT COUNT(*) FROM information_schema.schema_privileges WHERE grantee = account)
         + (SELECT COUNT(*) FROM information_schema.table_privileges WHERE grantee = account)
         + (SELECT COUNT(*) FROM information_schema.column_privileges WHERE grantee = account)
         + (SELECT COUNT(*) FROM mysql.procs_priv WHERE User = @user AND Host = '%')
         + (SELECT COUNT(*) FROM mysql.proxies_priv WHERE User = @user AND Host = '%')
         + (SELECT COUNT(*) FROM mysql.role_edges WHERE TO_USER = @user AND TO_HOST = '%')
      INTO left_over;
    IF left_over > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'a grant is left that could not be revoked; read SHOW GRANTS for the user';
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
