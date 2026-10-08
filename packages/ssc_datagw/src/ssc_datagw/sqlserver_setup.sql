-- SSC data gateway: the read-only login a SQL Server connection uses (GA-5).
--
-- Run it with sqlcmd (or SSMS in SQLCMD mode) on SQL Server 2022, as a sysadmin, in the
-- database the connection names. It makes the login and its user in that database, takes the
-- user out of every role, and grants SELECT on the schemas named; nothing else.
--
--   read -rs password && export password      # the login's password, typed, never in a file
--   sqlcmd -S db.example.com -d reporting -U admin -N -C -i sqlserver_setup.sql \
--          -v login=ssc_datagw schemas=reporting,finance
--   unset password
--
-- sqlcmd reads an environment variable as a scripting variable of the same name, so the
-- password never reaches the command line. In SSMS, put :setvar lines for login, schemas and
-- password above the script instead, and do not save them. The password may not hold a single
-- quote, and SQL Server's password policy applies (CHECK_POLICY = ON): it may not contain the
-- login's name.
--
-- Variables:
--   $(login)     the login's name, which is also its user's name in the database
--   $(schemas)   schemas whose tables and views the user may read, comma-separated
--   $(password)  the login's password
--
-- The script is safe to run again, which is how a new password or another schema lands. It
-- ends by acting as the user and stops with an error if the user may still write, alter or
-- execute anything in the database.

:on error exit
SET NOCOUNT ON;

DECLARE @login sysname = N'$(login)';
DECLARE @schemas nvarchar(max) = N'$(schemas)';
DECLARE @password nvarchar(128) = N'$(password)';
DECLARE @sql nvarchar(max);

IF @login = N'' OR @password = N'' OR LTRIM(RTRIM(@schemas)) = N''
    THROW 50001, N'login, schemas and password are required', 1;

-- The login: made, or given the new password.
SET @sql = CASE WHEN SUSER_ID(@login) IS NULL THEN N'CREATE' ELSE N'ALTER' END
    + N' LOGIN ' + QUOTENAME(@login)
    + N' WITH PASSWORD = ' + QUOTENAME(@password, N'''') + N', CHECK_POLICY = ON';
EXEC (@sql);
SET @sql = N'ALTER LOGIN ' + QUOTENAME(@login) + N' WITH DEFAULT_DATABASE = '
    + QUOTENAME(DB_NAME());
EXEC (@sql);

-- The login's user in this database.
DECLARE @user sysname = (SELECT name FROM sys.database_principals WHERE sid = SUSER_SID(@login));
IF @user = N'dbo'
    THROW 50002, N'the login owns the database; name another login', 1;
IF @user IS NULL
BEGIN
    SET @user = @login;
    SET @sql = N'CREATE USER ' + QUOTENAME(@user) + N' FOR LOGIN ' + QUOTENAME(@login);
    EXEC (@sql);
END;

-- Out of every server and database role.
SET @sql = N'';
SELECT @sql += N'ALTER SERVER ROLE ' + QUOTENAME(r.name) + N' DROP MEMBER '
    + QUOTENAME(@login) + N'; '
FROM sys.server_role_members AS m
JOIN sys.server_principals AS r ON r.principal_id = m.role_principal_id
WHERE m.member_principal_id = SUSER_ID(@login);
SELECT @sql += N'ALTER ROLE ' + QUOTENAME(r.name) + N' DROP MEMBER ' + QUOTENAME(@user) + N'; '
FROM sys.database_role_members AS m
JOIN sys.database_principals AS r ON r.principal_id = m.role_principal_id
WHERE m.member_principal_id = DATABASE_PRINCIPAL_ID(@user);
EXEC (@sql);

-- SELECT on each schema named.
DECLARE @missing sysname = (
    SELECT TOP (1) LTRIM(RTRIM(value)) FROM STRING_SPLIT(@schemas, N',')
    WHERE LTRIM(RTRIM(value)) <> N'' AND SCHEMA_ID(LTRIM(RTRIM(value))) IS NULL
);
IF @missing IS NOT NULL
    THROW 50003, N'a schema named in schemas does not exist in this database', 1;
SET @sql = N'';
SELECT @sql += N'GRANT SELECT ON SCHEMA::' + QUOTENAME(LTRIM(RTRIM(value))) + N' TO '
    + QUOTENAME(@user) + N'; '
FROM STRING_SPLIT(@schemas, N',')
WHERE LTRIM(RTRIM(value)) <> N'';
EXEC (@sql);

-- The check: as the user, nothing beyond reading.
DECLARE @wider int;
EXECUTE AS USER = @user;
SET @wider = ISNULL(IS_MEMBER(N'db_owner'), 1) + ISNULL(IS_MEMBER(N'db_datawriter'), 1)
    + ISNULL(IS_MEMBER(N'db_ddladmin'), 1)
    + ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), N'DATABASE', N'INSERT'), 1)
    + ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), N'DATABASE', N'UPDATE'), 1)
    + ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), N'DATABASE', N'DELETE'), 1)
    + ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), N'DATABASE', N'ALTER'), 1)
    + ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), N'DATABASE', N'CONTROL'), 1)
    + ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), N'DATABASE', N'EXECUTE'), 1)
    + (SELECT COUNT(*) FROM sys.database_permissions
       WHERE grantee_principal_id = DATABASE_PRINCIPAL_ID() AND state IN ('G', 'W')
         AND permission_name IN (N'INSERT', N'UPDATE', N'DELETE', N'ALTER', N'CONTROL',
                                 N'EXECUTE'));
REVERT;
IF @wider > 0
    THROW 50004, N'the user may still write, alter or execute: revoke those grants and run again', 1;

PRINT N'ready: the login may read the schemas named, and nothing else';
